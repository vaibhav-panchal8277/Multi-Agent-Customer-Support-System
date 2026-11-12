# Fixes Applied — Multi-Agent Customer Support System

> **Historical document.** This describes an earlier review pass. Several
> items were reworked afterwards — notably C3/C4 (auth is now per-session keys +
> a server-side `ADMIN_TOKEN` + verified customer identity, failing closed),
> refunds (customer confirmation required), guardrails (fail closed), tracing
> (one trace per turn incl. guardrails, background flush) and hand-off routing
> (loop guard, no internal text shown to customers). See `SECURITY.md` and the
> README for the current behaviour.

This document records the resolution of all 14 issues raised in `CODE_REVIEW.md`. Each
issue was fixed **individually**, then verified two ways: (1) the fix itself works, and
(2) it does not break any other component (re-running the earlier suites after each edit).

## How the fixes were verified

The backend's heavy dependencies (fastapi, motor, langgraph, langfuse,
groq, nemoguardrails) are not installed in the review sandbox and the
package proxy is blocked, so verification used three complementary techniques:

- **`py_compile`** on every backend module after every edit — catches syntax/indentation errors.
- **Standalone unit tests** for the pure `tools.py` functions (imported directly).
- **Extract-and-exec tests** for logic inside un-importable modules: the *real* source of
  a function (`run_turn`, `require_auth`, `lifespan`, `GroqLLM._call`,
  `_parse_confidence`, `_langfuse_host`, the CORS block) is regex-extracted from the file
  and executed against mocked primitives, so the assertions run against the shipped code,
  not a copy.

All four backend modules compile and all verification suites pass against the final code.

## Files changed

| File | What changed |
|---|---|
| `backend/tools.py` | C4 scoping layer, M1 relative dates, M2 refund re-validation, L4 UUID ids |
| `backend/agents.py` | C1 single-graph-invocation, C4 scoped tool calls, M3 non-blocking flush, M5 host fallback, L5 confidence parsing |
| `backend/server.py` | C3 bearer auth, C4 identity threading, M4 index creation, L1 input bounds, L2 CORS, L5 lifespan |
| `backend/guardrails_engine.py` | L3 loop-safe sync bridge + fail-open visibility |
| `frontend/src/lib/api.js` | C3 optional `Authorization: Bearer` header |
| `backend/.env`, `backend/env.example`, `frontend/.env` | C3 `API_TOKEN`/`REACT_APP_API_TOKEN`, M5 `LANGFUSE_HOST` |

> Note: `git status` shows ~108 files as changed. That is a pre-existing CRLF/LF
> line-ending artifact between the committed history (LF) and the working tree (CRLF), not
> real edits. Filtering with `git diff --ignore-space-at-eol` confirms only the files above
> were genuinely modified.

---

## Critical

### C1 — Multi-agent graph executed twice on the happy path
- **File/line:** `backend/agents.py`, `run_turn` (~line 440)
- **Problem:** The graph ran once inside the Langfuse `with` span *and* again at the
  function's unconditional `return await _GRAPH.ainvoke(...)`, doubling every LLM/tool call
  and cost on the normal path, and a tracing error re-ran the whole turn.
- **Fix:** Restructured so the graph is invoked **exactly once**. Tracing wraps only the
  bookkeeping around that single call: span-setup failure → untraced single run; graph
  error → close span and re-raise (no silent re-run); trace update/flush → best-effort.
- **Verified:** `ainvoke_calls == 1` across the tracing-on, tracing-off, span-setup-error,
  and update-error paths; result still returned; span always closed.

### C2 — Live secrets committed in `.env`
- **File:** `backend/.env`, `backend/env.example`
- **Problem:** Real-looking API keys were present in a tracked-style env file.
- **Fix:** Values redacted/blanked; `env.example` documents required keys with empty
  placeholders. Secrets were kept out of all command output during the work.
- **Verified:** No secret values remain in the example/template; app reads from env.

### C3 — All `/api` endpoints were unauthenticated
- **File/line:** `backend/server.py` (`require_auth` ~line 39, router ~line 83); `frontend/src/lib/api.js`
- **Problem:** Anyone who could reach the server could drive the agents and tools.
- **Fix:** A single shared bearer token (`API_TOKEN`) gates the whole `/api` surface via a
  router-level dependency, using `secrets.compare_digest` (constant-time). If `API_TOKEN`
  is unset the gate is disabled so **local dev is unchanged**. `/health` stays public. The
  frontend attaches `Authorization: Bearer <token>` when `REACT_APP_API_TOKEN` is set.
- **Verified:** Token unset → all requests allowed; token set → correct bearer allowed,
  while missing/empty/wrong/raw-token/wrong-case/trailing-space all return 401.

### C4 — No per-customer authorization (IDOR / cross-customer access)
- **File/line:** `backend/tools.py` (`call_tool_scoped` ~line 155); `backend/agents.py` (~line 315); `backend/server.py` (~line 267)
- **Problem:** Any session could look up, enumerate, or refund **any** customer's invoices;
  authorization was left to the LLM/prompt.
- **Fix:** Authorization is enforced **server-side** in `call_tool_scoped`, never by the
  LLM. When a session is bound to a `customer_email`: `list_invoices_for`'s email is
  **forced** to the session identity (no enumeration), and `lookup_invoice` /
  `check_refund_eligibility` / `process_refund` are **denied** unless the invoice belongs
  to that customer. Anonymous sessions (no bound email) run unrestricted, as before. The
  identity already existed on the session record and is now threaded end-to-end
  (session → `run_turn` → `ChatState` → `call_tool_scoped`).
- **Verified:** ACME's list never returns BETA rows; ACME cannot check or refund BETA's
  invoice; a non-existent invoice still reaches the tool's own "not found" (not a spurious
  auth error); technical tools are unaffected by scope.

---

## Medium

### M1 — Refund demo dates were hard-coded and would age out
- **File/line:** `backend/tools.py` (`MOCK_INVOICES` ~line 17)
- **Fix:** Issue dates are computed relative to now (`_days_ago(10/45/60)`), so INV-1001
  stays inside the 30-day window (eligible) and INV-1003 stays outside it (ineligible)
  regardless of the calendar date.
- **Verified:** INV-1001 → eligible; INV-1003 → outside-window; INV-1002 → not-paid.

### M2 — `process_refund` trusted the caller for eligibility and amount
- **File/line:** `backend/tools.py` (`process_refund` ~line 83)
- **Fix:** Re-checks eligibility server-side, **derives** the amount from the invoice
  (a supplied amount is accepted only if it matches exactly), and blocks duplicate refunds
  on the same invoice (idempotent).
- **Verified:** Wrong amount → `amount_mismatch`; eligible no-amount → processed with the
  invoice's true amount; second attempt → `already_refunded` (same original id); ineligible
  / non-existent → refused.

### M3 — Blocking Langfuse `flush()` on the event loop
- **File/line:** `backend/agents.py` (`run_turn`)
- **Fix:** `await asyncio.to_thread(_langfuse.flush)` moves the blocking network I/O off the
  event loop.
- **Verified:** Covered by the C1 suite (flush still occurs; loop not blocked).

### M4 — No MongoDB indexes for the app's query patterns
- **File/line:** `backend/server.py` (`lifespan` ~line 50)
- **Fix:** On startup, create indexes for `messages`/`turns` by `(session_id, created_at)`,
  a unique index on `sessions.id`, and `sessions.updated_at` desc. Index errors are logged
  but never block boot.
- **Verified:** All four indexes requested; a simulated index failure does **not** crash
  startup and shutdown still runs.

### M5 — `LANGFUSE_BASE_URL` vs `LANGFUSE_HOST` mismatch
- **File/line:** `backend/agents.py` (`_langfuse_host` ~line 27); `backend/.env`
- **Fix:** `_langfuse_host()` prefers `LANGFUSE_HOST`, falls back to the legacy
  `LANGFUSE_BASE_URL`, then to the default cloud host; `.env` standardized on `LANGFUSE_HOST`.
- **Verified:** Neither set → default; legacy key honored; `LANGFUSE_HOST` wins when both set.

---

## Low

### L1 — Unbounded chat input
- **File/line:** `backend/server.py` (`ChatIn` ~line 126)
- **Fix:** `session_id` (1–128) and `message` (1–4000) length constraints via pydantic `Field`.
- **Verified:** Declarative constraints; module compiles.

### L2 — CORS paired credentials with `*`
- **File/line:** `backend/server.py` (~line 350)
- **Fix:** Since auth is a bearer token (no cookies), credentials mode is only enabled when
  explicit origins are configured; a `*` (or unset) origin ships with credentials **off**,
  which is spec-legal.
- **Verified:** Unset/`*` → `["*"]` + credentials off; explicit origins → credentials on;
  mixed-with-`*` → treated as wildcard, credentials off.

### L3 — Guardrails sync bridge crashed under a running loop (silently failing open)
- **File/line:** `backend/guardrails_engine.py` (`GroqLLM._call` ~line 66)
- **Fix:** Detects a running event loop; if present, drives the coroutine to completion on a
  dedicated worker thread (`asyncio.run` there) instead of `get_event_loop().run_until_complete`.
  Fail-open events now increment a counter and record `last_error` for observability.
- **Verified:** Works both outside a loop and from **inside** a running loop (the old crash
  scenario) without "event loop is already running".

### L4 — Collision-prone `random.randint` ticket/refund ids
- **File/line:** `backend/tools.py` (`open_ticket` ~line 47, `process_refund` ~line 115)
- **Fix:** UUID-based ids (`TCK-XXXXXXXX` / `REF-XXXXXXXX`); `import random` removed.
- **Verified:** Ids match the collision-free format; no `random` reference remains anywhere.

### L5 — Deprecated `@app.on_event` + fragile confidence parsing
- **File/line:** `backend/server.py` (`lifespan`); `backend/agents.py` (`_parse_confidence` ~line 195)
- **Fix:** Shutdown moved into the `lifespan` context manager (the `on_event` decorator is
  gone). `_parse_confidence` coerces to a clamped `[0,1]` float and **preserves a genuine
  0.0** — the old `float(x or 0.5)` promoted 0.0 to 0.5 because 0.0 is falsy.
- **Verified:** 0.0 preserved; junk/None/NaN → 0.5 default; values clamped; no `on_event`
  decorator remains.
