# Code Review — MESH Support (Multi-Agent Customer Support System)

> **Historical document.** This describes an earlier review pass. Several
> items were reworked afterwards — notably C3/C4 (auth is now per-session keys +
> a server-side `ADMIN_TOKEN` + verified customer identity, failing closed),
> refunds (customer confirmation required), guardrails (fail closed), tracing
> (one trace per turn incl. guardrails, background flush) and hand-off routing
> (loop guard, no internal text shown to customers). See `SECURITY.md` and the
> README for the current behaviour.

_Reviewed: 2026-08-23. Scope: `backend/` (server, agents, tools, guardrails) + `frontend/` + config._

---

## 1. What this project is (plain-language tour)

MESH Support is a **customer-support chatbot that is actually a team of specialist bots working together**, with a safety layer and full activity logging.

**The flow of a single customer message:**

1. **Guardrails — input check.** Before anything else, the message is shown to a "safety classifier" LLM. It blocks jailbreak attempts ("ignore your instructions"), unsafe content, and off-topic questions (politics, trivia, coding help). If blocked, the customer gets a polite refusal and the rest of the system never runs.
2. **Supervisor.** If allowed, a "Supervisor" agent reads the message and classifies it into **billing**, **technical**, **refunds**, or **general**, with a confidence score. If confidence is low (< 0.55) or it's just small talk, it asks a **clarifying question** instead of guessing.
3. **Specialist sub-agent.** Otherwise it hands off to the matching specialist (Billing, Technical, or Refunds). Each specialist has its own instructions and its own **tools** — small mock functions like `lookup_invoice`, `open_ticket`, `check_refund_eligibility`. The specialist can call a tool, answer the customer, or hand back to the Supervisor if the question isn't really in its lane.
4. **Guardrails — output check.** The final reply is shown to the safety classifier again to make sure it isn't leaking secrets or being abusive before the customer sees it.
5. **Logging & storage.** Everything — every hop, every tool call, timing — is saved to **MongoDB** and also sent to **Langfuse** (an observability service). The React frontend shows a live "activity feed" per message, plus an `/admin` dashboard that visualizes each conversation's trace.

**The pieces, mapped to files:**

| File | Role |
|------|------|
| `backend/server.py` | The web server (FastAPI). Defines the API endpoints, saves messages/turns to MongoDB, and wires the guardrail checks around each chat turn. |
| `backend/agents.py` | The "brain." Builds the LangGraph state machine (Supervisor → specialists), calls the LLM, and wraps each turn in Langfuse tracing. |
| `backend/tools.py` | The fake back-office APIs: mock invoices, tickets, and a refund rules engine. Meant to be swapped for real Stripe/Zendesk later. |
| `backend/guardrails_engine.py` | The NeMo Guardrails safety layer (input/output content checks). |
| `backend/guardrails_config/*.yml` | The prompts and rail configuration for the safety checks. |
| `frontend/src/pages/ChatPage.jsx` | The customer chat UI. |
| `frontend/src/pages/AdminDashboard.jsx` | The internal trace/metrics dashboard. |
| `frontend/src/lib/api.js` | Thin wrapper around the backend HTTP API. |

The technology is genuinely modern (LangGraph 1.x, FastAPI, React 19, NeMo Guardrails, Langfuse) and the structure is clean. The issues below are mostly about **production-readiness** — auth, data scoping, cost/latency, and one real orchestration bug — not about the overall design, which is sound.

---

## 2. Critical issues

Severity key: 🔴 High · 🟠 Medium · 🟡 Lower / hardening

---

### 🔴 C1 — The whole agent graph runs **twice** on any error (correctness + cost)

- **File / lines:** `backend/agents.py`, lines **432–475** (the graph call at line **444** sits inside the tracing `try`; the fallback re-invoke is line **475**).
- **Problem:** `await _GRAPH.ainvoke(...)` is *inside* the `try/except` that is only meant to protect the Langfuse tracing wrapper. If the graph itself raises — an LLM error that survives retries, a `GraphRecursionError`, or any bug in a node — the `except` catches it, logs "Langfuse span failed," and then **re-runs the entire multi-agent graph** at line 475. Consequences:
  - Transient error → every LLM call in the turn happens **twice** (double latency, double token cost).
  - Deterministic error (e.g. recursion limit hit) → runs twice and still throws → unhandled **500** to the user, after doing 2× the work.
  - The reused `initial` state may carry mutated `trace_events`/`tool_calls` into the second run.
- **Fix:** Run the graph exactly once, *outside* the tracing guard. Only wrap the span setup/update/flush in `try/except`:
  ```python
  final = await _GRAPH.ainvoke(initial, config={"recursion_limit": 20})  # once
  if _langfuse is not None:
      try:
          with _langfuse.start_as_current_observation(name="support_turn", as_type="span",
                                                       input=..., metadata=...) as obs:
              obs.update(output=..., metadata=...)
              obs.set_trace_io(input=..., output=...)
          await asyncio.to_thread(_langfuse.flush)
      except Exception as e:
          log.warning("Langfuse trace failed: %s", e)
  return final
  ```

---

### 🔴 C2 — Live API credentials stored in `backend/.env`

- **File / lines:** `backend/.env` — line **10** `GROQ_API_KEY`, line **15** `LANGFUSE_SECRET_KEY`, line **16** `LANGFUSE_PUBLIC_KEY`.
- **Problem:** These hold **real-looking secret values** (e.g. `GROQ_API_KEY="gsk_…"`, `LANGFUSE_SECRET_KEY="sk-lf-e2cf5df2-…"`). Good news, verified: `.env` **is** correctly git-ignored and is **not** committed (`git ls-files` shows no `.env`). But the secrets sit in plaintext in the project folder, and the README explicitly encourages zipping/sharing the project — so they are easy to leak. Anyone with the folder can bill your Groq account and read/write your Langfuse project.
- **Fix:** **Rotate these keys now** (Groq API key + Langfuse secret) since they've been exposed in plaintext. Keep only placeholders (as in `env.example`) in any copy you share. Confirm `.env` stays git-ignored (it is). For deployment, inject secrets via the platform's env/secret manager rather than a file.

---

### 🔴 C3 — No authentication or authorization on any endpoint (broken access control)

- **File / lines:** `backend/server.py` — every route, e.g. `GET /sessions` **150–153**, `GET /sessions/{sid}` **156–163**, `GET /sessions/{sid}/traces` **166–170**, `DELETE /sessions/{sid}` **293–298**.
- **Problem:** All endpoints are fully public. `GET /api/sessions` returns **every conversation for every customer** (titles, `customer_email`, counts). Anyone who can reach the API can list, read full histories, mark resolved, and **delete** any session; the `/admin` dashboard is equally open. There is no per-customer scoping anywhere. (The README lists auth as a TODO — flagging it because the project bills itself as "production-shaped.")
- **Fix:** Add authentication (JWT or session) and authorization. Scope every session/message/turn query to the authenticated user (filter by an owner/`customer_email`). Put `/admin` behind an admin role. At minimum, require a token on list and state-changing endpoints.

---

### 🔴 C4 — Tools return any customer's data by identifier — no ownership check (IDOR pattern)

- **File / lines:** `backend/tools.py` — `lookup_invoice` **21–25**, `list_invoices_for` **28–33**; invoked in `backend/agents.py` `_make_subagent_node` **287–297** with LLM-supplied `args`.
- **Problem:** The sub-agent feeds whatever `invoice_id`/`email` the **LLM extracted from the user's text** straight into the tool. `list_invoices_for(email)` returns invoices for **any** email; `lookup_invoice(id)` returns any invoice including its owner's email. Nothing verifies the requester owns that data. A user can say *"list invoices for beta@example.com"* and receive another customer's billing records. (The guardrails eval set even marks that exact request as ALLOW.) Harmless against the mock, but this is a direct IDOR against a real billing backend.
- **Fix:** Bind each session to an authenticated customer identity **server-side**, pass that identity into the tools (not via the LLM), and filter/deny results that don't belong to that customer.

---

### 🟠 M1 — Refund flow is effectively dead for the shipped demo data (correctness)

- **File / lines:** `backend/tools.py` — `check_refund_eligibility` 30-day rule at **56–59**, against mock invoices at **6–13**.
- **Problem:** All mock invoices are dated Jan–Feb 2026, and the rule rejects refunds older than 30 days. As of today (2026-08-23) every invoice is 200+ days old, so `check_refund_eligibility` **always** returns `eligible: False (outside_30d_window)`. The README's own refund demo ("refund INV-1001, I cancelled last week") can never succeed. (The output-rail eval even hardcodes "213 days old," acknowledging this.)
- **Fix:** Generate demo dates relative to now, e.g. `"issued": (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()` for at least one invoice, so both eligible and ineligible paths are exercised. Or make the window/`now` configurable for demos.

---

### 🟠 M2 — `process_refund` trusts the caller's amount and skips eligibility (data integrity)

- **File / lines:** `backend/tools.py` — `process_refund` **67–70**.
- **Problem:** It issues a refund for whatever `amount` the LLM passes, for any invoice, **without** re-checking eligibility or that the amount matches the invoice total. Nothing prevents refunding an arbitrary amount, refunding an unpaid/ineligible invoice, or double-refunding the same one. Against a real payment API this is a money-loss bug.
- **Fix:** Re-run `check_refund_eligibility` **inside** `process_refund` (server is the source of truth), derive `amount` from the invoice rather than the caller, and record processed refunds to prevent duplicates.

---

### 🟠 M3 — Blocking Langfuse flush every turn + extra guardrail round-trips (performance / scaling)

- **File / lines:** `backend/agents.py` **464** (`_langfuse.flush()` per turn inside async `run_turn`); guardrail calls in `backend/server.py` **197** (`check_input`) and **223** (`check_output`).
- **Problem:** `_langfuse.flush()` is a **synchronous, blocking network call executed on the async event loop** for every chat request — it stalls all concurrent requests and defeats the SDK's batching. Each turn already makes ~4 **serial** LLM calls (input rail → supervisor → sub-agent, plus any tool loop → output rail); a blocking flush adds another network hop on the hot path. Under load this serializes requests and inflates p95 latency and cost.
- **Fix:** Drop the per-turn flush and rely on the SDK's background batching (a shutdown flush already exists in `server.py` **318**). If you must flush, offload it: `await asyncio.to_thread(_langfuse.flush)`. Consider running the input rail concurrently with early work, and skipping the output rail for clearly low-risk replies.

---

### 🟠 M4 — No MongoDB indexes → collection scans as data grows (scaling)

- **File / lines:** queries in `backend/server.py` **152**, **161–162**, **168–169**, **180–182** (all filter by `session_id` and sort by `created_at`/`updated_at`); no index creation anywhere in the codebase (verified).
- **Problem:** Every query filters by `session_id` and sorts on a timestamp with no supporting index. As `messages`/`turns` grow, these become full collection scans with in-memory sorts — slow, and large sorts eventually error out.
- **Fix:** Create indexes on startup: `messages` and `turns` → compound `{session_id: 1, created_at: 1}`; `sessions` → unique `{id: 1}` and `{updated_at: -1}`. Use Motor's `create_index` in a startup/lifespan hook.

---

### 🟠 M5 — `LANGFUSE_BASE_URL` in `.env` is never read (config bug)

- **File / lines:** `backend/.env` line **15** sets `LANGFUSE_BASE_URL`, but `backend/agents.py` **33** & **46** read `LANGFUSE_HOST` (which is what `env.example` **23** correctly uses).
- **Problem:** The host variable in the real `.env` has the wrong name, so it is silently ignored and the host always falls back to the default `https://us.cloud.langfuse.com`. It works today only because the default matches the intended value; point it at an EU/self-hosted cluster and traces would silently go to the wrong place or fail auth.
- **Fix:** Rename `LANGFUSE_BASE_URL` → `LANGFUSE_HOST` in `.env` (or have the code accept both names).

---

### 🟡 L1 — No length limit on chat input (DoS / cost)

- **File / lines:** `backend/server.py` — `ChatIn` **74–76**, used by `/chat`.
- **Problem:** `message: str` has no maximum length. A client can post a multi-megabyte message that gets persisted, embedded into prompts, and sent to the LLM — memory pressure, cost blow-up, slow turns.
- **Fix:** Constrain it: `message: str = Field(min_length=1, max_length=4000)` and reject oversized input early.

---

### 🟡 L2 — CORS: `allow_credentials=True` combined with wildcard origins (hardening)

- **File / lines:** `backend/server.py` **303–309** (`allow_origins` defaults to `*` from `CORS_ORIGINS`, with `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`).
- **Problem:** Wildcard origin + credentials is an anti-pattern (Starlette reflects the request origin when cookies are present, effectively allowing credentialed requests from *any* site). There's no auth/cookies today so real-world risk is currently low — but the moment the planned auth is added, this becomes a CSRF/credential-exposure vector.
- **Fix:** In production set `CORS_ORIGINS` to your exact frontend origin(s); only enable credentials when actually needed, and never pair `*` with credentials.

---

### 🟡 L3 — Guardrails sync `_call` will crash under a running loop; fail-open hides it (robustness / security)

- **File / lines:** `backend/guardrails_engine.py` **64–68** (`asyncio.get_event_loop().run_until_complete(...)`), fail-open handler **150–154**.
- **Problem:** If NeMo ever invokes the synchronous `_call` while the event loop is running (normal in an async server), `run_until_complete` raises "event loop is already running." That error is swallowed by the fail-open handler, so the guardrail **silently allows everything**. `get_event_loop()` is also deprecated. More broadly, fail-open means any guard error or misconfiguration disables safety silently.
- **Fix:** Make `_call` safe (raise `NotImplementedError` to force the async path, or guard with `asyncio.get_running_loop()` detection). Decide deliberately whether jailbreak protection should fail-open vs. fail-closed with a short timeout, and alert on rail errors so a silently-disabled guard is visible.

---

### 🟡 L4 — Random IDs collide; global mutable mock state (data integrity, mock-only)

- **File / lines:** `backend/tools.py` — `open_ticket` **36–39** (`random.randint(600, 999)` → overwrites an existing ticket on collision), `process_refund` **67**; module-level `MOCK_INVOICES`/`MOCK_TICKETS` **6–18**.
- **Problem:** Only 400 possible ticket IDs; a collision silently overwrites a prior ticket. The global dicts aren't shared across uvicorn workers and reset on restart. Acceptable for a demo, noted for correctness.
- **Fix:** Use `uuid4` for generated IDs; persist tickets to the DB if they need to survive restarts / multiple workers.

---

### 🟡 L5 — Deprecated shutdown hook + brittle confidence parsing

- **File / lines:** `backend/server.py` **316** (`@app.on_event("shutdown")` is deprecated in favor of lifespan handlers); `backend/agents.py` **192** — `float(result.get("confidence", 0.5) or 0.5)`.
- **Problem:** The confidence coercion turns a genuine `0.0` into `0.5` (the `or` treats 0 as falsy), and if the LLM returns a non-numeric value like `"high"`, `float(...)` raises `ValueError` — which is uncaught in the node and would trip the C1 double-run/500 path.
- **Fix:** Move startup/shutdown to a `lifespan` handler. Parse confidence defensively: `try/except`, default safely, and clamp to `[0.0, 1.0]`.

---

## 3. What's already good

- **No XSS in the frontend** — message content is rendered as React children (`{msg.content}`, `{t.response}`), so it's auto-escaped; no `dangerouslySetInnerHTML` anywhere (verified). `withCredentials` is not set, so the CORS issue is latent rather than active.
- **`.env` is properly git-ignored** and not committed; `env.example` ships safe placeholders.
- **Retry/backoff** on LLM calls (`_with_retries`) and **hop caps** (`hops >= 4`) + `recursion_limit=20` bound the agent loop.
- **Guardrails fail-open** and **Langfuse errors are swallowed** by design, so observability/safety outages can't take chat down (a reasonable tradeoff — just be intentional about the guardrail case, see L3).
- Clean separation of concerns, typed graph state, and a genuinely useful trace timeline.

---

_Line numbers refer to the files as reviewed on 2026-08-23._
