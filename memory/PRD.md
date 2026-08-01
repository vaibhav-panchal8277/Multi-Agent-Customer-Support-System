# Multi-Agent Customer Support Platform — PRD

## Problem statement (verbatim)
Multi-agent customer support platform. Central Supervisor Agent classifies incoming queries and routes them to specialised sub-agents (Billing, Technical Support, Refunds), each with its own tools, prompt, and scope. Preserves context across handoffs, persists conversation memory, streams a visual timeline of every hop/tool call for observability.

## Architecture
- **Backend**: FastAPI (`/app/backend/server.py`), LangGraph state machine (`agents.py`), mock tool APIs (`tools.py`), MongoDB persistence.
- **LLM**: GPT-5.4-mini via emergentintegrations `LlmChat` (Emergent Universal Key).
- **Tracing**: Langfuse v3 SDK, `start_as_current_span` per turn.
- **State**: LangGraph `StateGraph(ChatState)` with nodes {supervisor, clarify, billing, technical, refunds}. Conditional edges enable dynamic handoff between sub-agents and back to supervisor.
- **Frontend**: React + Tailwind, dark "Cybernetic Command Center" aesthetic, agent-colored accents.
  - `/` and `/c/:sid` — customer chat with activity feed per response.
  - `/admin` — bento-grid dashboard with agent distribution, tool-call count, per-turn trace timeline.

## Core requirements (implemented, 2026-02-01)
- Supervisor classifies intent (billing/technical/refund/general) with structured JSON output.
- Sub-agents call registered mock tools (`lookup_invoice`, `list_invoices_for`, `open_ticket`, `get_ticket`, `check_refund_eligibility` with 30-day rules engine, `process_refund`).
- Sub-agents can hand back to Supervisor mid-conversation with a summary.
- Low-confidence supervisor routes → clarifying question node (fallback path).
- Session, messages, and per-turn trace events persisted in Mongo (`sessions`, `messages`, `turns`).
- Langfuse spans wrap every turn with session_id, intent, confidence, tool_calls in metadata.
- Frontend visualises: agent badge with color glow, activity feed, tool receipt JSON, admin trace timeline.
- Retry with exponential backoff on LLM rate-limit errors.

## User personas
- **Customer**: reaches the chat via `/`, gets responses tagged with the responding agent + inline trace of what happened.
- **Support ops / admin**: opens `/admin`, inspects any conversation's trace timeline to debug routing/tool decisions.

## Test credentials
N/A — open demo, no auth.

## Backlog (P0/P1/P2)
- P1: Streaming responses via SSE (currently non-streaming for reliability).
- P1: Human-escalation node when a sub-agent handoff loop exceeds threshold.
- P2: Real (non-mock) integrations for invoices/tickets (Stripe / Zendesk).
- P2: Langfuse trace deep-link from admin dashboard.
- P2: Auth (JWT or Emergent Google) so admin dashboard is gated.
