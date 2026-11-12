# MESH Support — Multi-Agent Customer Support Platform

A production-shaped reference implementation of a multi-agent support system.
A **Supervisor Agent** classifies every incoming customer message and hands it
off to a specialist sub-agent (**Billing**, **Technical Support**, **Refunds**),
each with its own prompt and tool set. Conversation state persists across
hand-offs so the customer never has to repeat themselves, and every hop / tool
call is traced through **Langfuse** for full observability. Every turn is wrapped
in **NeMo Guardrails** input/output rails that block jailbreaks, off-topic
requests, and unsafe replies.

<p align="center">
  <em>Guardrails → Supervisor → routes → Billing / Technical / Refunds → tools → Guardrails → reply, all traced.</em>
</p>

---

## Highlights

- **LangGraph** state machine — explicit nodes + conditional edges instead of hidden agent self-reasoning.
- **Context-carrying hand-offs** — every agent sees the last few messages; on a hand-off the receiving agent (or the Supervisor) also gets the previous agent's summary, and a loop guard stops agents bouncing a message between each other.
- **Confidence-gated fallback** — low-confidence classifications trigger a clarifying question instead of a blind route.
- **NeMo Guardrails** — LLM-based input rail (jailbreak / prompt-injection + off-topic filtering) and output rail (moderation), running on the same Groq LLM (GPT-OSS 120B). Blocks short-circuit the graph and surface as red shield events in the timeline.
- **Mock tool APIs** — invoices, tickets, refund rules engine (`tools.py`). Swap for real Stripe / Zendesk when ready.
- **Fail-closed access control** — per-session keys, a verified customer identity before any invoice/refund lookup, refunds that only execute on the customer's explicit confirmation, and a separate server-side admin token. See [`SECURITY.md`](./SECURITY.md).
- **Full observability** — Langfuse v3 spans per turn + local trace timeline surfaced in an admin dashboard.
- **Dark "Command Center" UI** — customer chat with per-message activity feed + `/admin` bento dashboard.

---

## Tech stack

| Layer            | Tech                                                          |
|------------------|---------------------------------------------------------------|
| Orchestration    | LangGraph 1.x                                                 |
| LLM              | GPT-OSS 120B (`openai/gpt-oss-120b`) via the Groq API (`groq` SDK) |
| Guardrails       | NeMo Guardrails 0.23 (self-check input/output rails)          |
| Backend          | FastAPI + Motor (async MongoDB)                               |
| Memory / state   | MongoDB (`sessions`, `messages`, `turns` collections)         |
| Observability    | Langfuse v3 (Python SDK)                                      |
| Frontend         | React 19, Tailwind, shadcn/ui, lucide-react, sonner           |
| Tooling layer    | In-memory mock REST APIs (see `backend/tools.py`)             |

---

## Project layout

```
.
├── backend/
│   ├── server.py          # FastAPI routes, Mongo persistence
│   ├── agents.py          # LangGraph StateGraph + Langfuse tracing
│   ├── tools.py           # Mock invoices / tickets / refund rules engine
│   ├── guardrails_engine.py       # NeMo Guardrails engine + Groq LLM wrapper
│   ├── guardrails_config/         # NeMo config
│   │   ├── config.yml             #   rails: self check input / output
│   │   └── prompts.yml            #   input/output safety prompts
│   ├── tests/             # E2E suites (live server) + offline test_tools_scoping.py
│   ├── requirements.txt
│   └── env.example
├── frontend/
│   ├── src/
│   │   ├── pages/ChatPage.jsx
│   │   ├── pages/AdminDashboard.jsx
│   │   ├── components/{AgentBadge,MessageCard,TraceTimeline}.jsx
│   │   └── lib/api.js
│   └── package.json
├── SECURITY.md            # Access model, guardrail failure policy, known gaps
├── design_guidelines.json # UI/UX blueprint used by the design agent
├── memory/PRD.md          # Product doc (persona, backlog, status)
└── README.md
```

---

## Getting started (local)

### 1. Prerequisites
- Python 3.11–3.13 (NeMo Guardrails 0.23 does not install on Python 3.14 yet)
- Node 18+ and Yarn (**not npm**)
- MongoDB — either installed locally, or the provided `docker-compose.yml`

### 2. Clone + configure
```bash
git clone <your-repo-url> mesh-support
cd mesh-support

# --- Backend ---
# The template ships as `env.example` (no leading dot, always visible).
# Copy it and rename to `.env` (the actual file the app loads):
cp backend/env.example backend/.env         # mac / linux
# ren backend\env.example backend\.env      # windows cmd

# then edit backend/.env and fill in:
#   GROQ_API_KEY               (Groq API key from console.groq.com)
#   LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST
#   ADMIN_TOKEN                (needed to open /admin; never put it in frontend/.env)

# --- Frontend ---
echo 'REACT_APP_BACKEND_URL=http://localhost:8001' > frontend/.env
```

### 3. Start MongoDB (skip if you already have one running)
```bash
docker compose up -d          # starts mongo on localhost:27017
```

### 4. Install
```bash
# Backend
cd backend
pip install -r requirements.txt        # installs the groq SDK + deps from PyPI

# Frontend
cd ../frontend
yarn install
```

### 5. Run
```bash
# Terminal 1 — backend
cd backend
uvicorn server:app --host 0.0.0.0 --port 8001 --reload

# Terminal 2 — frontend
cd frontend
yarn start
```

Open **http://localhost:3000** for the chat and **http://localhost:3000/admin** for the trace
dashboard (it asks for your `ADMIN_TOKEN`).

To let the agents look up invoices or refunds, press **Verify account** in the chat
header and sign in with a demo identity:

| Email              | PIN    |
|--------------------|--------|
| `acme@example.com` | `4821` |
| `beta@example.com` | `7305` |

Refunds are proposed by the Refunds agent and only execute when you press
**Confirm refund** under its reply.

---

## How the graph works

```
  ┌─────────────────────────┐      started together; tools wait
  │   Guardrails: INPUT     │◄ ─ ─ for the input rail's verdict
  │ jailbreak/off-topic gate │
  └──────────┬──────────────┘
             │ blocked → graph cancelled, polite refusal
             ▼
              ┌─────────────────────────┐
              │      Supervisor         │
              │  (classify + route)     │
              └──────────┬──────────────┘
                         │
        ┌────────────┬───┴────┬────────────┐
        ▼            ▼        ▼            ▼
    Billing     Technical  Refunds     Clarify
   (invoices)   (tickets) (rules eng.) (fallback)
        │            │        │
        └────────┬───┴────────┘
                 │  handoff back to Supervisor if
                 │  the query drifts out of scope
                 ▼
              ┌─────────────────────────┐
              │   Guardrails: OUTPUT    │
              │  moderate final reply   │
              └──────────┬──────────────┘
                         ▼
                        END
```

- The **input rail** and the graph start together, so the rail's LLM round trip doesn't add to every turn. The graph's reasoning runs speculatively, but **every tool call waits for the rail's verdict**; if the rail blocks, the graph is cancelled, no tool runs, and the customer gets a polite refusal (`agent="guardrail"`). Trade-off: a message that ends up blocked may still have been sent to the agent LLM (its output is discarded).
- Each sub-agent may (a) call a tool, (b) answer the customer, or (c) hand off to another specialist or back to the Supervisor.
- A hand-off carries the sending agent's **summary** to the next agent / Supervisor / clarifying question.
- **Loop guard:** a specialist that already handled the message this turn is never routed to again; the Supervisor asks the customer instead. After `MAX_HOPS` (4) tool calls / hand-offs the customer gets a fixed fallback reply. Internal hand-off notes are never shown to the customer.
- Confidence < 0.55 → Supervisor asks a clarifying question instead of guessing.
- The **output rail** moderates the final reply before it reaches the user.

Read `backend/agents.py` for the full definition; every node updates a
`ChatState` TypedDict and appends a structured `trace_events` array that the
frontend renders as an activity feed.

---

## API surface

Auth column: **public** = no extra header; **key** = `X-Session-Key` of that
session (or `X-Admin-Token`); **admin** = `X-Admin-Token`. If `API_TOKEN` is set,
every `/api` call also needs `Authorization: Bearer <API_TOKEN>`.

| Method | Route                                            | Auth   | Purpose                                         |
|--------|--------------------------------------------------|--------|-------------------------------------------------|
| GET    | `/api/`                                          | public | Health, model info                              |
| GET    | `/api/agents`                                    | public | List sub-agents + colors                        |
| POST   | `/api/sessions`                                  | public | Create a conversation; returns its `session_key` once |
| POST   | `/api/sessions/mine`                             | key(s) | The caller's own sessions (`{"sessions":[{"id","key"}]}`) |
| GET    | `/api/sessions/{id}`                             | key    | Session detail + messages + refund statuses     |
| POST   | `/api/sessions/{id}/verify`                      | key    | Bind a verified customer (`{"email","pin"}`)    |
| POST   | `/api/chat`                                      | key    | Send a message, run the graph                   |
| POST   | `/api/sessions/{id}/refunds/{confirmation_id}/confirm` | key | Execute a refund the agent proposed       |
| POST   | `/api/sessions/{id}/resolve`                     | key    | Mark ticket resolved (chat then returns 409)    |
| POST   | `/api/sessions/{id}/reopen`                      | key    | Reopen a resolved ticket                        |
| DELETE | `/api/sessions/{id}`                             | key    | Delete session + messages                       |
| GET    | `/api/sessions`                                  | admin  | List all conversations                          |
| GET    | `/api/sessions/{id}/traces`                      | admin  | Per-turn trace events (admin dashboard)         |
| GET    | `/api/langfuse/status`                           | admin  | Langfuse auth / flush diagnostics               |
| GET    | `/api/guardrails/status`                         | admin  | NeMo Guardrails state + block counters          |

**Rate limits** (per minute, in-process; `0` disables): `CHAT_RATE_LIMIT_PER_MIN`
per session (default 20) and `SESSION_RATE_LIMIT_PER_MIN` new conversations per
client IP (default 30). Over the limit the API returns `429` with `Retry-After`.
With several workers/replicas each keeps its own counters — move to a shared
store (e.g. Redis) for real abuse protection.

`POST /api/chat` response shape:

```jsonc
{
  "session_id": "…",
  "agent": "billing",
  "agent_color": "#10b981",
  "response": "…natural language reply…",
  "intent": "billing",
  "confidence": 0.98,
  "trace_events": [
    { "agent": "supervisor", "type": "classify", "intent": "billing", "confidence": 0.98, "latency_ms": 811 },
    { "agent": "supervisor", "type": "handoff", "to": "billing" },
    { "agent": "billing", "type": "tool_call", "tool": "lookup_invoice", "args": { "invoice_id": "INV-1002" }, "result": { "…": "…" } },
    { "agent": "billing", "type": "final" }
  ],
  "tool_calls": [ /* same tool receipts, flat */ ]
}
```

---

## Observability with Langfuse

Each `POST /api/chat` is **one trace**, tagged with the conversation's
`session_id`:

```
support_turn                      span      (opened in server.py around the whole turn)
├─ guardrail.input                guardrail verdict, latency
├─ agent_graph                    chain     intent, confidence, hops, tools used
│  ├─ agent.supervisor            agent
│  │  └─ supervisor.llm           generation  prompt, raw output, token usage
│  ├─ agent.billing               agent     (one per node visit)
│  │  ├─ billing.llm              generation
│  │  └─ tool.lookup_invoice      tool      args, result
│  └─ …
└─ guardrail.output               guardrail
```

Open your Langfuse project → **Traces** → each row is one turn. Filter by
session to reconstruct a whole conversation. Spans are sent by a background
flush after the response, so tracing never adds latency to the customer.

Langfuse errors are swallowed by design — if the SaaS is unreachable, chat
keeps working (and the graph is never re-run). Errors show up in
`GET /api/langfuse/status` (`last_error`). Langfuse is connected at server
startup, not on import.

---

## Guardrails with NeMo

Every `POST /api/chat` turn is wrapped in [NeMo Guardrails](https://github.com/NVIDIA-NeMo/Guardrails):

- **Input rail** (`self check input`) — an LLM classifier blocks jailbreak /
  prompt-injection attempts, unsafe content, and **off-topic** messages (anything
  outside billing / technical / refunds / general support). If it blocks, the
  LangGraph never runs and the customer gets a polite refusal.
- **Output rail** (`self check output`) — moderates the assistant's final answer
  before it reaches the user. It is deliberately tuned to **allow** the customer's
  own account/invoice/refund data (that's the job) while blocking system-prompt or
  credential leaks, abuse, and unsafe content.

The rails run on the **same Groq GPT-OSS 120B** — it's wrapped as a LangChain
`LLM` and injected directly:

```python
from nemoguardrails import LLMRails, RailsConfig

cfg = RailsConfig.from_path("guardrails_config")
rails = LLMRails(cfg, llm=GroqLLM(api_key=GROQ_API_KEY))

# input rail only, before the graph runs
result = await rails.generate_async(
    messages=[{"role": "user", "content": user_message}],
    options=GenerationOptions(rails=["input"], log={"activated_rails": True}),
)
```

Config lives in `backend/guardrails_config/` (`config.yml` + `prompts.yml`). The
rails are **LLM-only** (no embeddings / vector index needed) and
`enable_rails_exceptions` gives a deterministic block signal. A blocked turn
returns `agent="guardrail"` and emits a red shield `guardrail` trace event that
renders in both the chat activity feed and the `/admin` timeline. Blocked turns
are kept out of the history the agents see on later turns, so a rejected prompt
injection can't be replayed.

Transient Groq errors are retried. If a rail still can't run, it **fails closed**
(the turn is blocked with a "temporarily unavailable" reply); set
`GUARDRAILS_FAIL_MODE=open` to allow on error in local development. Check
`GET /api/guardrails/status` (admin) for `enabled`, `fail_mode`, and the
`checks_run` / `blocks` / `errors` / `retries` counters. Optional env
`GUARDRAILS_MODEL` overrides the guardrail model (defaults to `openai/gpt-oss-120b`).

---

## Try it (curl)

```bash
API=http://localhost:8001/api

# 1. create a session — keep its key, it is only returned once
RESP=$(curl -s -X POST $API/sessions -H 'Content-Type: application/json' -d '{"title":"demo"}')
SID=$(echo "$RESP" | jq -r .id)
KEY=$(echo "$RESP" | jq -r .session_key)
H=(-H 'Content-Type: application/json' -H "X-Session-Key: $KEY")

# 2. Verify as a demo customer (required for invoice / refund tools)
curl -s -X POST $API/sessions/$SID/verify "${H[@]}" \
  -d '{"email":"acme@example.com","pin":"4821"}' | jq

# 3. Billing query — should call lookup_invoice
curl -s -X POST $API/chat "${H[@]}" \
  -d "{\"session_id\":\"$SID\",\"message\":\"I was double-charged for INV-1002\"}" | jq

# 4. Refund query — 30-day rules engine; the agent only *proposes* the refund
CID=$(curl -s -X POST $API/chat "${H[@]}" \
  -d "{\"session_id\":\"$SID\",\"message\":\"Refund INV-1001, cancelled last week\"}" \
  | jq -r '.tool_calls[] | select(.tool=="request_refund") | .result.confirmation_id // empty')

# 5. Customer confirms -> refund executes (once)
curl -s -X POST $API/sessions/$SID/refunds/$CID/confirm "${H[@]}" | jq

# 6. Guardrail block — off-topic message never reaches the agents
curl -s -X POST $API/chat "${H[@]}" \
  -d "{\"session_id\":\"$SID\",\"message\":\"Who should I vote for in the election?\"}" | jq
# -> agent: "guardrail", intent: "blocked", polite refusal
```

Mock invoices you can play with (see `backend/tools.py`):

| Invoice   | Customer            | Amount  | Status   | Issued     |
|-----------|---------------------|---------|----------|------------|
| INV-1001  | acme@example.com    | $79.00  | paid     | 2026-01-05 |
| INV-1002  | acme@example.com    | $79.00  | overdue  | 2026-02-05 |
| INV-1003  | beta@example.com    | $199.00 | paid     | 2026-01-15 |

---

## Testing

```bash
# Backend — offline: real FastAPI app on an in-memory Mongo, fake LLM/guardrails.
# No API keys, network or MongoDB needed. Includes real-NeMo integration tests
# (skipped automatically where NeMo isn't installed, e.g. Python 3.14).
cd backend && pip install -r requirements.txt && pytest

# Backend — live end-to-end (real Groq + Langfuse) against a running server:
REACT_APP_BACKEND_URL=http://localhost:8001 ADMIN_TOKEN=... pytest

# Frontend — Jest (jsdom), API client + chat/admin pages:
cd frontend && CI=true yarn test --watchAll=false
```

Evaluation harnesses (live LLM calls, print a scorecard): `backend/evaluate.py`
(routing/tools), `backend/evaluate_guardrails.py` (rail precision/recall) and
`backend/evaluate_project.py` (whole pipeline over HTTP; set `BASE_URL`).

---

## Deployment

- Backend: any container platform (Render, Railway, Fly.io, ECS). Expose port `8001` and set the env vars from `backend/env.example` (including a strong `ADMIN_TOKEN`).
- Frontend: build with `yarn build` and serve statics anywhere (Vercel, Netlify, S3+CloudFront, Nginx). Set `REACT_APP_BACKEND_URL` **at build time** to your public backend URL.
- MongoDB: MongoDB Atlas free tier works fine; drop the connection string into `MONGO_URL`.

---

## Roadmap

- [x] NeMo Guardrails input/output rails (jailbreak, off-topic, moderation)
- [ ] SSE streaming of agent responses (word-by-word)
- [ ] Human-escalation node when the graph loops
- [ ] Real Stripe test refunds behind the rules engine
- [ ] Deep-link each admin turn into its Langfuse trace
- [ ] Real identity provider instead of the demo email + PIN directory
- [ ] JWT / Google OAuth for the admin dashboard

---

## License

MIT — see [`LICENSE`](./LICENSE).
