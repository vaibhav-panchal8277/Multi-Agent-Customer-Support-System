"""FastAPI backend for multi-agent customer support platform."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# import agents AFTER env is loaded
from agents import AGENT_COLORS, LANGFUSE_STATUS, flush_langfuse, run_turn  # noqa: E402
from guardrails_engine import GUARDRAILS_STATUS, check_input, check_output  # noqa: E402

# --- Mongo ---
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="Multi-Agent Support API")
api = APIRouter(prefix="/api")


# ---------- Models ----------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guardrail_event(which: str, r: Dict[str, Any]) -> Dict[str, Any]:
    """Build a trace-timeline event for a guardrail check."""
    blocked = bool(r.get("blocked"))
    label_prefix = "Input rail" if which == "input" else "Output rail"
    label = f"{label_prefix} {'blocked' if blocked else 'passed'}"
    return {
        "agent": "guardrail",
        "type": "guardrail",
        "label": label,
        "blocked": blocked,
        "reason": r.get("reason"),
        "rail": r.get("rail"),
        "latency_ms": r.get("latency_ms"),
        "color": "#ef4444" if blocked else "#22d3ee",
        "ts": time.time(),
    }


class SessionCreate(BaseModel):
    title: Optional[str] = None
    customer_email: Optional[str] = None


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    title: str
    customer_email: Optional[str] = None
    status: str = "open"
    created_at: str
    updated_at: str
    last_agent: Optional[str] = None
    message_count: int = 0


class ChatIn(BaseModel):
    session_id: str
    message: str


class ChatOut(BaseModel):
    session_id: str
    agent: str
    agent_color: str
    response: str
    intent: Optional[str] = None
    confidence: Optional[float] = None
    trace_events: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []


# ---------- Routes ----------
@api.get("/")
async def root():
    return {"ok": True, "service": "multi-agent-support", "model": "gpt-5.4-mini"}


@api.get("/langfuse/status")
async def langfuse_status():
    """Diagnostic endpoint: shows whether Langfuse auth succeeded and how many
    traces have been flushed since backend start. If auth_ok is false, your
    LANGFUSE_HOST likely doesn't match the cluster where you created the keys.
    """
    return LANGFUSE_STATUS


@api.get("/guardrails/status")
async def guardrails_status():
    """Diagnostic endpoint: NeMo Guardrails state + counters (checks run / blocks)."""
    return GUARDRAILS_STATUS


@api.get("/agents")
async def list_agents():
    return {
        "agents": [
            {"key": "supervisor", "name": "Supervisor",
             "color": AGENT_COLORS["supervisor"],
             "scope": "Classifies intent, routes, asks clarifying questions."},
            {"key": "billing", "name": "Billing",
             "color": AGENT_COLORS["billing"],
             "scope": "Invoices, charges, subscription plans, payments."},
            {"key": "technical", "name": "Technical Support",
             "color": AGENT_COLORS["technical"],
             "scope": "Bugs, integrations, product errors, tickets."},
            {"key": "refunds", "name": "Refunds",
             "color": AGENT_COLORS["refunds"],
             "scope": "Refund eligibility (rules engine) and processing."},
        ]
    }


@api.post("/sessions", response_model=SessionOut)
async def create_session(body: SessionCreate):
    sid = str(uuid.uuid4())
    now = _now()
    doc = {
        "id": sid,
        "title": body.title or "New conversation",
        "customer_email": body.customer_email,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "last_agent": None,
        "message_count": 0,
    }
    await db.sessions.insert_one(doc)
    doc.pop("_id", None)
    return SessionOut(**doc)


@api.get("/sessions", response_model=List[SessionOut])
async def list_sessions():
    rows = await db.sessions.find({}, {"_id": 0}).sort("updated_at", -1).to_list(200)
    return [SessionOut(**r) for r in rows]


@api.get("/sessions/{sid}")
async def get_session(sid: str):
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
    messages = await db.messages.find({"session_id": sid}, {"_id": 0}) \
        .sort("created_at", 1).to_list(1000)
    return {"session": session, "messages": messages}


@api.get("/sessions/{sid}/traces")
async def get_traces(sid: str):
    rows = await db.turns.find({"session_id": sid}, {"_id": 0}) \
        .sort("created_at", 1).to_list(500)
    return {"turns": rows}


@api.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn):
    session = await db.sessions.find_one({"id": body.session_id}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found. create one first via POST /api/sessions")

    # load history
    history_docs = await db.messages.find(
        {"session_id": body.session_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(200)
    history = [{"role": m["role"], "content": m["content"]} for m in history_docs]

    # persist user message
    user_msg = {
        "id": str(uuid.uuid4()),
        "session_id": body.session_id,
        "role": "user",
        "content": body.message,
        "agent": None,
        "created_at": _now(),
    }
    await db.messages.insert_one(dict(user_msg))

    # ---- Guardrails: input rail (block jailbreak / unsafe / off-topic) ----
    gr_in = await check_input(body.message)
    trace_events: List[Dict[str, Any]] = [_guardrail_event("input", gr_in)]

    if gr_in.get("blocked"):
        response_text = (
            "I'm sorry, but I can only help with billing, technical, refund, and "
            "account questions — so I can't help with that request. Is there something "
            "about your account, an invoice, or a recent order I can look into?"
        )
        final_agent = "guardrail"
        intent = "blocked"
        confidence = None
        tool_calls: List[Dict[str, Any]] = []
        hops = 0
    else:
        # run graph
        state = await run_turn(body.session_id, body.message, history)
        response_text = state.get("response", "…")
        final_agent = state.get("current_agent_final") or "supervisor"
        intent = state.get("intent")
        confidence = state.get("confidence")
        tool_calls = state.get("tool_calls", [])
        hops = state.get("hops", 0)
        trace_events = trace_events + state.get("trace_events", [])

        # ---- Guardrails: output rail (moderate the final answer) ----
        gr_out = await check_output(body.message, response_text)
        trace_events = trace_events + [_guardrail_event("output", gr_out)]
        if gr_out.get("blocked"):
            response_text = (
                "I'm sorry — I wasn't able to produce a response I can safely share here. "
                "If this is urgent I can escalate you to a human agent."
            )
            final_agent = "guardrail"

    agent_color = "#ef4444" if final_agent == "guardrail" else AGENT_COLORS.get(final_agent, "#A1A1AA")

    # persist assistant message
    asst_msg = {
        "id": str(uuid.uuid4()),
        "session_id": body.session_id,
        "role": "assistant",
        "content": response_text,
        "agent": final_agent,
        "agent_color": agent_color,
        "intent": intent,
        "confidence": confidence,
        "trace_events": trace_events,
        "tool_calls": tool_calls,
        "created_at": _now(),
    }
    await db.messages.insert_one(dict(asst_msg))

    # persist turn trace (for admin dashboard)
    turn_doc = {
        "id": str(uuid.uuid4()),
        "session_id": body.session_id,
        "user_message": body.message,
        "response": response_text,
        "agent": final_agent,
        "intent": intent,
        "confidence": confidence,
        "trace_events": trace_events,
        "tool_calls": tool_calls,
        "hops": hops,
        "created_at": _now(),
    }
    await db.turns.insert_one(dict(turn_doc))

    # update session summary
    await db.sessions.update_one(
        {"id": body.session_id},
        {"$set": {"updated_at": _now(), "last_agent": final_agent},
         "$inc": {"message_count": 2}},
    )

    return ChatOut(
        session_id=body.session_id,
        agent=final_agent,
        agent_color=agent_color,
        response=response_text,
        intent=intent,
        confidence=confidence,
        trace_events=trace_events,
        tool_calls=tool_calls,
    )


@api.post("/sessions/{sid}/resolve")
async def resolve(sid: str):
    r = await db.sessions.update_one({"id": sid}, {"$set": {"status": "resolved", "updated_at": _now()}})
    if r.matched_count == 0:
        raise HTTPException(404, "not found")
    return {"ok": True}


@api.delete("/sessions/{sid}")
async def delete_session(sid: str):
    await db.sessions.delete_one({"id": sid})
    await db.messages.delete_many({"session_id": sid})
    await db.turns.delete_many({"session_id": sid})
    return {"ok": True}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def _shutdown():
    flush_langfuse()
    client.close()
