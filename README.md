# MESH Support — Multi-Agent Customer Support Platform

A production-shaped reference implementation of a multi-agent support system.
A **Supervisor Agent** classifies every incoming customer message and hands it
off to a specialist sub-agent (**Billing**, **Technical Support**, **Refunds**),
each with its own prompt and tool set. Conversation state persists across
hand-offs so the customer never has to repeat themselves, and every hop / tool
call is traced through **Langfuse** for full observability.

<p align="center">
  <em>Supervisor → routes → Billing / Technical / Refunds → tools → reply, all traced.</em>
</p>

---

## Highlights

- **LangGraph** state machine — explicit nodes + conditional edges instead of hidden agent self-reasoning.
- **Per-agent isolated memory** — only a summary is forwarded on hand-off, keeping token cost predictable.
- **Confidence-gated fallback** — low-confidence classifications trigger a clarifying question instead of a blind route.
- **Mock tool APIs** — invoices, tickets, refund rules engine (`tools.py`). Swap for real Stripe / Zendesk when ready.
- **Full observability** — Langfuse v3 spans per turn + local trace timeline surfaced in an admin dashboard.
- **Dark "Command Center" UI** — customer chat with per-message activity feed + `/admin` bento dashboard.

---

## Tech stack

| Layer            | Tech                                                          |
|------------------|---------------------------------------------------------------|
| Orchestration    | LangGraph 1.x                                                 |
| LLM              | GPT-5.4-mini via `emergentintegrations` (Emergent Universal Key) |
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
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── pages/ChatPage.jsx
│   │   ├── pages/AdminDashboard.jsx
│   │   ├── components/{AgentBadge,MessageCard,TraceTimeline}.jsx
│   │   └── lib/api.js
│   ├── package.json
│   └── .env.example
├── design_guidelines.json # UI/UX blueprint used by the design agent
├── memory/PRD.md          # Product doc (persona, backlog, status)
└── README.md
```

---

## Getting started (local)

### 1. Prerequisites
- Python 3.11+
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
#   EMERGENT_LLM_KEY           (Emergent Universal Key)
#   LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST

# --- Frontend ---
cp frontend/env.example frontend/.env       # mac / linux
# ren frontend\env.example frontend\.env    # windows cmd
# REACT_APP_BACKEND_URL defaults to http://localhost:8001 for local dev
```

> **Why two names?** `backend/.env.example` (dotfile) and `backend/env.example`
> (visible) have identical content — dotfiles get filtered out by some ZIP
> tools and file managers, so we ship both. Use whichever you can see.

### 3. Start MongoDB (skip if you already have one running)
```bash
docker compose up -d          # starts mongo on localhost:27017
```

### 4. Install
```bash
# Backend
cd backend
pip install -r requirements.txt
pip install emergentintegrations \
  --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/

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

Open **http://localhost:3000** for the chat and **http://localhost:3000/admin** for the trace dashboard.

---

## How the graph works

```
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
                END
```

- Each sub-agent may (a) call a tool, (b) answer the customer, or (c) hand back to Supervisor.
- Hand-off carries a **short summary**, not the full history — keeps context lean.
- Confidence < 0.55 → Supervisor asks a clarifying question instead of guessing.

Read `backend/agents.py` for the full definition; every node updates a
`ChatState` TypedDict and appends a structured `trace_events` array that the
frontend renders as an activity feed.

---

## API surface

| Method | Route                              | Purpose                                    |
|--------|------------------------------------|--------------------------------------------|
| GET    | `/api/`                            | Health, model info                         |
| GET    | `/api/agents`                      | List sub-agents + colors                   |
| POST   | `/api/sessions`                    | Create a new conversation                  |
| GET    | `/api/sessions`                    | List all conversations                     |
| GET    | `/api/sessions/{id}`               | Session detail + messages                  |
| GET    | `/api/sessions/{id}/traces`        | Per-turn trace events (admin dashboard)    |
| POST   | `/api/chat`                        | Send a message, run the graph              |
| POST   | `/api/sessions/{id}/resolve`       | Mark ticket resolved                       |
| DELETE | `/api/sessions/{id}`               | Delete session + messages                  |

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

Every `POST /api/chat` is wrapped in a Langfuse span:

```python
with _langfuse.start_as_current_span(
    name="support_turn",
    input={"user_message": msg, "session_id": sid},
) as span:
    final = await GRAPH.ainvoke(state)
    span.update_trace(
        session_id=sid,
        output={"agent": final["agent"], "response": final["response"]},
        metadata={
            "intent": final["intent"],
            "confidence": final["confidence"],
            "tool_calls": final["tool_calls"],
        },
    )
```

Open your Langfuse project → **Traces** → each row is one turn. Filter by
`session_id` to reconstruct a whole conversation.

Langfuse errors are swallowed by design — if the SaaS is unreachable, chat
keeps working.

---

## Try it (curl)

```bash
API=http://localhost:8001/api

# 1. create a session
SID=$(curl -s -X POST $API/sessions -H 'Content-Type: application/json' \
  -d '{"title":"demo"}' | jq -r .id)

# 2. Billing query — should call lookup_invoice
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SID\",\"message\":\"I was double-charged for INV-1002\"}" | jq

# 3. Refund query — 30-day rules engine
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SID\",\"message\":\"Refund INV-1001, cancelled last week\"}" | jq
```

Mock invoices you can play with (see `backend/tools.py`):

| Invoice   | Customer            | Amount  | Status   | Issued     |
|-----------|---------------------|---------|----------|------------|
| INV-1001  | acme@example.com    | $79.00  | paid     | 2026-01-05 |
| INV-1002  | acme@example.com    | $79.00  | overdue  | 2026-02-05 |
| INV-1003  | beta@example.com    | $199.00 | paid     | 2026-01-15 |

---

## Deployment

- Backend: any container platform (Render, Railway, Fly.io, ECS). Expose port `8001` and set the env vars from `backend/.env.example`.
- Frontend: build with `yarn build` and serve statics anywhere (Vercel, Netlify, S3+CloudFront, Nginx). Set `REACT_APP_BACKEND_URL` **at build time** to your public backend URL.
- MongoDB: MongoDB Atlas free tier works fine; drop the connection string into `MONGO_URL`.

---

## Roadmap

- [ ] SSE streaming of agent responses (word-by-word)
- [ ] Human-escalation node when the graph loops
- [ ] Real Stripe test refunds behind the rules engine
- [ ] Deep-link each admin turn into its Langfuse trace
- [ ] JWT / Emergent-managed Google auth for the admin dashboard

---

## License

MIT — see [`LICENSE`](./LICENSE).
