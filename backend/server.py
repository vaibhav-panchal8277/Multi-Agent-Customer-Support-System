"""FastAPI backend for multi-agent customer support platform."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# import agents AFTER env is loaded
from agents import (AGENT_COLORS, LANGFUSE_STATUS, MODEL_NAME, flush_langfuse,  # noqa: E402
                    init_tracing, observe, run_turn, trace_turn)
from guardrails_engine import (GUARDRAILS_STATUS, check_input, check_output,  # noqa: E402
                               init_guardrails)
from ratelimit import SlidingWindowLimiter  # noqa: E402
from tools import execute_confirmed_refund, verify_customer  # noqa: E402

log = logging.getLogger(__name__)

# --- Mongo ---
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

# --- Auth ---------------------------------------------------------------------
# Three independent layers:
#
# 1. API_TOKEN (optional) — a shared client key the frontend sends as
#    `Authorization: Bearer ...`. It is baked into the public JS bundle, so it is
#    NOT a security boundary; it only keeps casual traffic off the API.
# 2. Per-session key — POST /api/sessions returns a random key exactly once and
#    stores only its SHA-256 hash. Every request that reads or changes a
#    conversation must send it as `X-Session-Key`. This is what stops one
#    customer from reading, chatting in, refunding through, or deleting someone
#    else's conversation.
# 3. ADMIN_TOKEN — a server-side secret (never put it in the frontend build),
#    sent as `X-Admin-Token`, for cross-session endpoints: list every session,
#    traces, diagnostics. Unset => admin endpoints are disabled (fail closed).
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

PENDING_REFUND_TTL_S = 15 * 60   # a proposed refund must be confirmed within 15 min
MAX_VERIFY_FAILURES = 5          # wrong email/PIN attempts before a session locks
HISTORY_TURNS = 20               # most recent messages loaded as agent context


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Per-minute limits (0 disables). In-process only: see ratelimit.py.
_chat_limiter = SlidingWindowLimiter(_int_env("CHAT_RATE_LIMIT_PER_MIN", 20), 60)
_session_limiter = SlidingWindowLimiter(_int_env("SESSION_RATE_LIMIT_PER_MIN", 30), 60)


def _enforce(limiter: SlidingWindowLimiter, key: str, what: str) -> None:
    retry = limiter.hit(key)
    if retry:
        raise HTTPException(429, f"too many {what}; please slow down",
                            headers={"Retry-After": str(math.ceil(retry))})

# Never return these session fields to a client.
_SESSION_PROJECTION = {"_id": 0, "key_hash": 0, "verify_failures": 0}


def _digest_eq(a: str, b: str) -> bool:
    # bytes compare: str compare_digest raises on non-ASCII header values
    return secrets.compare_digest(a.encode(), b.encode())


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _is_admin(x_admin_token: Optional[str]) -> bool:
    return bool(ADMIN_TOKEN) and bool(x_admin_token) and _digest_eq(x_admin_token, ADMIN_TOKEN)


async def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """Require `Authorization: Bearer <API_TOKEN>` when a token is configured."""
    if not API_TOKEN:
        return
    if not _digest_eq(authorization or "", f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="invalid or missing API token")


async def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(403, "admin endpoints are disabled: set ADMIN_TOKEN on the server")
    if not _is_admin(x_admin_token):
        raise HTTPException(401, "invalid or missing admin token")


def _public(session: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in session.items() if k not in _SESSION_PROJECTION}


async def _load_session(sid: str, x_session_key: Optional[str],
                        x_admin_token: Optional[str]) -> Dict[str, Any]:
    """Fetch a session the caller is allowed to access (its key holder or an admin)."""
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
    if _is_admin(x_admin_token):
        return session
    stored = session.get("key_hash")
    if not stored or not x_session_key or not _digest_eq(_hash_key(x_session_key), stored):
        raise HTTPException(403, "missing or invalid session key")
    return session


async def session_access(sid: str,
                         x_session_key: Optional[str] = Header(default=None),
                         x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Dependency for /sessions/{sid}/... routes."""
    return await _load_session(sid, x_session_key, x_admin_token)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # ---- Startup: fail fast on missing config, then connect external services.
    # These used to run at import time (blocking network I/O, KeyError on import).
    if not os.environ.get("GROQ_API_KEY", "").strip():
        raise RuntimeError("GROQ_API_KEY is not set (see backend/env.example)")
    await asyncio.to_thread(init_tracing)       # Langfuse auth_check is blocking I/O
    await asyncio.to_thread(init_guardrails)    # NeMo config load

    # ---- Ensure indexes for this API's query patterns (issue M4) ----
    try:
        await db.messages.create_index([("session_id", 1), ("created_at", 1)],
                                        name="messages_session_created")
        await db.turns.create_index([("session_id", 1), ("created_at", 1)],
                                    name="turns_session_created")
        await db.sessions.create_index("id", unique=True, name="sessions_id_unique")
        await db.sessions.create_index([("updated_at", -1)],
                                       name="sessions_updated_desc")
        await db.pending_refunds.create_index("confirmation_id", unique=True,
                                              name="pending_refunds_id_unique")
        log.info("MongoDB indexes ensured")
    except Exception as e:  # don't let an index hiccup stop the app from booting
        log.warning("MongoDB index creation failed: %s", e)

    try:
        yield
    finally:
        # ---- Shutdown (replaces deprecated @app.on_event, issue L5) ----
        flush_langfuse()
        client.close()


app = FastAPI(title="Multi-Agent Support API", lifespan=lifespan)


@app.get("/health")
async def health():
    """Public, unauthenticated liveness probe (no token required)."""
    return {"ok": True, "auth_required": bool(API_TOKEN), "admin_enabled": bool(ADMIN_TOKEN)}


api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ---------- Models ----------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guardrail_event(which: str, r: Dict[str, Any]) -> Dict[str, Any]:
    """Build a trace-timeline event for a guardrail check."""
    blocked = bool(r.get("blocked"))
    label_prefix = "Input rail" if which == "input" else "Output rail"
    if r.get("unavailable"):
        outcome = "unavailable (blocked, fail-closed)" if blocked else "unavailable (allowed, fail-open)"
    else:
        outcome = "blocked" if blocked else "passed"
    return {
        "agent": "guardrail",
        "type": "guardrail",
        "label": f"{label_prefix} {outcome}",
        "blocked": blocked,
        "reason": r.get("reason"),
        "rail": r.get("rail"),
        "latency_ms": r.get("latency_ms"),
        "color": "#ef4444" if blocked else "#22d3ee",
        "ts": time.time(),
    }


class SessionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    # Admin-only: pre-bind a customer identity (e.g. a back-office agent opening
    # a conversation for a known customer). Customers use POST /verify instead.
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


class SessionCreated(SessionOut):
    # Returned exactly once; the server only keeps its hash.
    session_key: str


class SessionKeyRef(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=256)


class MySessionsIn(BaseModel):
    sessions: List[SessionKeyRef] = Field(default_factory=list, max_length=100)


class VerifyIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    pin: str = Field(min_length=1, max_length=32)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    # Bound the message so a client can't push an unbounded payload into the LLM
    # prompt / DB (issue L1). 4000 chars is generous for support chat.
    message: str = Field(min_length=1, max_length=4000)


class ChatOut(BaseModel):
    session_id: str
    agent: str
    agent_color: str
    response: str
    intent: Optional[str] = None
    confidence: Optional[float] = None
    trace_events: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []


INPUT_BLOCKED_TEXT = (
    "I'm sorry, but I can only help with billing, technical, refund, and "
    "account questions — so I can't help with that request. Is there something "
    "about your account, an invoice, or a recent order I can look into?"
)
GUARDRAIL_UNAVAILABLE_TEXT = (
    "Sorry — our safety check is temporarily unavailable, so I can't process "
    "messages right now. Please try again in a moment."
)
OUTPUT_BLOCKED_TEXT = (
    "I'm sorry — I wasn't able to produce a response I can safely share here. "
    "If this is urgent I can escalate you to a human agent."
)


# ---------- Public routes ----------
@api.get("/")
async def root():
    return {"ok": True, "service": "multi-agent-support", "model": MODEL_NAME}


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


@api.post("/sessions", response_model=SessionCreated)
async def create_session(body: SessionCreate, request: Request,
                         x_admin_token: Optional[str] = Header(default=None)):
    if not _is_admin(x_admin_token):
        # Uses the direct peer address. Behind a reverse proxy, configure
        # uvicorn's --forwarded-allow-ips so this is the real client IP.
        _enforce(_session_limiter, request.client.host if request.client else "unknown",
                 "new conversations")
    if body.customer_email and not _is_admin(x_admin_token):
        raise HTTPException(403, "customer_email can only be bound by an admin; "
                                 "customers verify via POST /api/sessions/{id}/verify")
    sid = str(uuid.uuid4())
    key = secrets.token_urlsafe(32)
    now = _now()
    doc = {
        "id": sid,
        "title": body.title or "New conversation",
        "customer_email": (body.customer_email or "").strip().lower() or None,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "last_agent": None,
        "message_count": 0,
        "key_hash": _hash_key(key),
        "verify_failures": 0,
    }
    await db.sessions.insert_one(dict(doc))
    return SessionCreated(**_public(doc), session_key=key)


@api.post("/sessions/mine", response_model=List[SessionOut])
async def my_sessions(body: MySessionsIn):
    """Sessions the caller holds keys for (the customer's own sidebar)."""
    keys = {ref.id: ref.key for ref in body.sessions}
    if not keys:
        return []
    rows = await db.sessions.find({"id": {"$in": list(keys)}}, {"_id": 0}) \
        .sort("updated_at", -1).to_list(len(keys))
    return [SessionOut(**_public(r)) for r in rows
            if r.get("key_hash") and _digest_eq(_hash_key(keys[r["id"]]), r["key_hash"])]


# ---------- Per-session routes (session key or admin) ----------
@api.get("/sessions/{sid}")
async def get_session(session: Dict[str, Any] = Depends(session_access)):
    sid = session["id"]
    messages = await db.messages.find({"session_id": sid}, {"_id": 0}) \
        .sort("created_at", 1).to_list(1000)
    refunds = await db.pending_refunds.find(
        {"session_id": sid}, {"_id": 0, "confirmation_id": 1, "status": 1}
    ).to_list(200)
    return {"session": _public(session), "messages": messages,
            "refunds": {r["confirmation_id"]: r["status"] for r in refunds}}


@api.post("/sessions/{sid}/verify", response_model=SessionOut)
async def verify_identity(body: VerifyIn, session: Dict[str, Any] = Depends(session_access)):
    """Bind a verified customer identity to the session (email + PIN).

    Until this succeeds, tools that touch customer-owned data (invoices,
    refunds) refuse to run for this session.
    """
    sid = session["id"]
    if session.get("verify_failures", 0) >= MAX_VERIFY_FAILURES:
        raise HTTPException(429, "too many failed attempts; start a new conversation")
    email = verify_customer(body.email, body.pin)
    if email is None:
        await db.sessions.update_one({"id": sid}, {"$inc": {"verify_failures": 1}})
        raise HTTPException(401, "email or PIN not recognised")
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {"customer_email": email, "verify_failures": 0, "updated_at": _now()}},
    )
    updated = await db.sessions.find_one({"id": sid}, _SESSION_PROJECTION)
    return SessionOut(**updated)


@api.post("/sessions/{sid}/refunds/{confirmation_id}/confirm")
async def confirm_refund(confirmation_id: str,
                         session: Dict[str, Any] = Depends(session_access)):
    """Execute a refund the Refunds agent proposed, on the customer's explicit click.

    This is the only path that moves money; the LLM can only propose.
    """
    sid = session["id"]
    pending = await db.pending_refunds.find_one(
        {"confirmation_id": confirmation_id, "session_id": sid}, {"_id": 0})
    if not pending:
        raise HTTPException(404, "refund request not found")
    if pending["status"] != "pending":
        raise HTTPException(409, f"refund request already {pending['status']}")
    if time.time() > pending.get("expires_at", 0):
        await db.pending_refunds.update_one({"confirmation_id": confirmation_id},
                                            {"$set": {"status": "expired"}})
        raise HTTPException(410, "refund request expired; please ask again")
    # The identity that requested it must still be the session's identity.
    if not session.get("customer_email") or pending.get("customer_email") != session["customer_email"]:
        raise HTTPException(403, "refund was requested under a different account")

    # Atomically claim it so a double click can't execute twice.
    claimed = await db.pending_refunds.find_one_and_update(
        {"confirmation_id": confirmation_id, "status": "pending"},
        {"$set": {"status": "processing"}},
    )
    if not claimed:
        raise HTTPException(409, "refund request is already being processed")

    result = execute_confirmed_refund(pending["invoice_id"], session["customer_email"])
    status = "confirmed" if result.get("ok") else "failed"
    await db.pending_refunds.update_one(
        {"confirmation_id": confirmation_id},
        {"$set": {"status": status, "result": result, "decided_at": _now()}},
    )

    if result.get("ok"):
        text = (f"Your refund {result['refund_id']} of ${result['amount']:.2f} for "
                f"{result['invoice_id']} has been processed.")
    else:
        text = (f"I couldn't process the refund for {pending['invoice_id']} "
                f"({result.get('error')}). Please contact support if this looks wrong.")
    msg = {
        "id": str(uuid.uuid4()),
        "session_id": sid,
        "role": "assistant",
        "content": text,
        "agent": "refunds",
        "agent_color": AGENT_COLORS["refunds"],
        "tool_calls": [{"agent": "refunds", "tool": "process_refund",
                        "args": {"invoice_id": pending["invoice_id"]}, "result": result}],
        "trace_events": [],
        "created_at": _now(),
    }
    await db.messages.insert_one(dict(msg))
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {"updated_at": _now(), "last_agent": "refunds"}, "$inc": {"message_count": 1}},
    )
    return {"ok": bool(result.get("ok")), "status": status, "result": result, "message": msg}


async def _checked(which: str, check, *args) -> Dict[str, Any]:
    """Run one guardrail check inside a ``guardrail.<which>`` observation."""
    with observe(f"guardrail.{which}", as_type="guardrail", input=args[-1]) as obs:
        verdict = await check(*args)
        obs.update(output=verdict, level="WARNING" if verdict.get("blocked") else None)
        return verdict


async def _guarded_turn(session: Dict[str, Any], message: str,
                        history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Input rail ∥ agent graph -> output rail for one message.

    The input rail and the graph start together, so the rail's LLM round trip no
    longer adds to every turn's latency. Safety is kept by a gate: the graph's
    LLM reasoning runs speculatively, but every tool call waits for the rail and
    only runs if it passed. If the rail blocks, the graph is cancelled and its
    output discarded.
    """
    sid = session["id"]
    t: Dict[str, Any] = {"tool_calls": [], "hops": 0, "confidence": None}

    gate: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
    # run graph (bind the session's verified identity so tools are scoped)
    graph_task = asyncio.create_task(run_turn(
        sid, message, history, customer_email=session.get("customer_email"), tool_gate=gate))

    # ---- Guardrails: input rail (block jailbreak / unsafe / off-topic) ----
    try:
        gr_in = await _checked("input", check_input, message)
    except BaseException:
        gate.set_result(False)
        graph_task.cancel()
        raise
    t["trace_events"] = [_guardrail_event("input", gr_in)]
    t["blocked"] = bool(gr_in.get("blocked"))
    gate.set_result(not t["blocked"])
    if t["blocked"]:
        graph_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await graph_task
        t["response_text"] = GUARDRAIL_UNAVAILABLE_TEXT if gr_in.get("unavailable") else INPUT_BLOCKED_TEXT
        t["final_agent"] = "guardrail"
        t["intent"] = "guardrail_unavailable" if gr_in.get("unavailable") else "blocked"
        return t

    try:
        state = await graph_task
    except Exception:
        # Nothing has been persisted for this turn yet, so the conversation
        # stays consistent and the customer can simply retry.
        log.exception("graph failed for session %s", sid)
        raise HTTPException(502, "the support agents failed to respond; please try again") from None
    t["response_text"] = state.get("response", "…")
    t["final_agent"] = state.get("current_agent_final") or "supervisor"
    t["intent"] = state.get("intent")
    t["confidence"] = state.get("confidence")
    t["tool_calls"] = state.get("tool_calls", [])
    t["hops"] = state.get("hops", 0)
    t["trace_events"] += state.get("trace_events", [])

    # ---- Guardrails: output rail (moderate the final answer) ----
    gr_out = await _checked("output", check_output, message, t["response_text"])
    t["trace_events"].append(_guardrail_event("output", gr_out))
    if gr_out.get("blocked"):
        t["response_text"] = OUTPUT_BLOCKED_TEXT
        t["final_agent"] = "guardrail"
        t["blocked"] = True
    return t


@api.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn,
               x_session_key: Optional[str] = Header(default=None),
               x_admin_token: Optional[str] = Header(default=None)):
    session = await _load_session(body.session_id, x_session_key, x_admin_token)
    sid = session["id"]
    if session.get("status") == "resolved":
        raise HTTPException(409, "this conversation is resolved; reopen it to continue")
    _enforce(_chat_limiter, sid, "messages")
    user_created_at = _now()

    # Agent context: the most recent messages that PASSED the guardrails, in
    # chronological order. Blocked turns are excluded so a rejected prompt
    # injection can't be replayed to the agents on a later, harmless turn.
    history_docs = await db.messages.find(
        {"session_id": sid, "blocked": {"$ne": True}},
        {"_id": 0, "role": 1, "content": 1},
    ).sort("created_at", -1).limit(HISTORY_TURNS).to_list(HISTORY_TURNS)
    history_docs.reverse()
    history = [{"role": m["role"], "content": m["content"]} for m in history_docs]

    # One Langfuse trace per turn, covering the guardrails as well as the graph.
    with trace_turn(sid, body.message) as turn:
        t = await _guarded_turn(session, body.message, history)
        summary = {"agent": t["final_agent"], "response": t["response_text"]}
        # the root observation's input/output is what the Traces list shows
        turn.update(output=summary, metadata={
            "intent": t["intent"], "confidence": t["confidence"], "hops": t["hops"],
            "blocked": t["blocked"], "tool_calls": [tc.get("tool") for tc in t["tool_calls"]]})
    response_text, final_agent = t["response_text"], t["final_agent"]
    intent, confidence, hops = t["intent"], t["confidence"], t["hops"]
    tool_calls, trace_events, blocked = t["tool_calls"], t["trace_events"], t["blocked"]

    # Proposed refunds wait for the customer's explicit confirmation.
    if not blocked:
        for tc in tool_calls:
            res = tc.get("result")
            if (tc.get("tool") == "request_refund" and isinstance(res, dict)
                    and res.get("status") == "pending_confirmation"):
                await db.pending_refunds.insert_one({
                    "confirmation_id": res["confirmation_id"],
                    "session_id": sid,
                    "invoice_id": res["invoice_id"],
                    "amount": res.get("amount"),
                    "customer_email": session.get("customer_email"),
                    "status": "pending",
                    "created_at": _now(),
                    "expires_at": time.time() + PENDING_REFUND_TTL_S,
                })

    agent_color = "#ef4444" if final_agent == "guardrail" else AGENT_COLORS.get(final_agent, "#A1A1AA")

    # persist the user + assistant messages together (blocked turns flagged)
    user_msg = {
        "id": str(uuid.uuid4()),
        "session_id": sid,
        "role": "user",
        "content": body.message,
        "agent": None,
        "blocked": blocked,
        "created_at": user_created_at,
    }
    asst_msg = {
        "id": str(uuid.uuid4()),
        "session_id": sid,
        "role": "assistant",
        "content": response_text,
        "agent": final_agent,
        "agent_color": agent_color,
        "intent": intent,
        "confidence": confidence,
        "trace_events": trace_events,
        "tool_calls": tool_calls,
        "blocked": blocked,
        "created_at": _now(),
    }
    await db.messages.insert_many([dict(user_msg), dict(asst_msg)])

    # persist turn trace (for admin dashboard)
    turn_doc = {
        "id": str(uuid.uuid4()),
        "session_id": sid,
        "user_message": body.message,
        "response": response_text,
        "agent": final_agent,
        "intent": intent,
        "confidence": confidence,
        "trace_events": trace_events,
        "tool_calls": tool_calls,
        "hops": hops,
        "blocked": blocked,
        "created_at": _now(),
    }
    await db.turns.insert_one(dict(turn_doc))

    # update session summary
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {"updated_at": _now(), "last_agent": final_agent},
         "$inc": {"message_count": 2}},
    )

    return ChatOut(
        session_id=sid,
        agent=final_agent,
        agent_color=agent_color,
        response=response_text,
        intent=intent,
        confidence=confidence,
        trace_events=trace_events,
        tool_calls=tool_calls,
    )


@api.post("/sessions/{sid}/resolve")
async def resolve(session: Dict[str, Any] = Depends(session_access)):
    await db.sessions.update_one({"id": session["id"]},
                                 {"$set": {"status": "resolved", "updated_at": _now()}})
    return {"ok": True}


@api.post("/sessions/{sid}/reopen", response_model=SessionOut)
async def reopen(session: Dict[str, Any] = Depends(session_access)):
    await db.sessions.update_one({"id": session["id"]},
                                 {"$set": {"status": "open", "updated_at": _now()}})
    return SessionOut(**await db.sessions.find_one({"id": session["id"]}, _SESSION_PROJECTION))


@api.delete("/sessions/{sid}")
async def delete_session(session: Dict[str, Any] = Depends(session_access)):
    sid = session["id"]
    await db.sessions.delete_one({"id": sid})
    await db.messages.delete_many({"session_id": sid})
    await db.turns.delete_many({"session_id": sid})
    await db.pending_refunds.delete_many({"session_id": sid})
    return {"ok": True}


# ---------- Admin routes (X-Admin-Token) ----------
admin = APIRouter(prefix="/api", dependencies=[Depends(require_auth), Depends(require_admin)])


@admin.get("/sessions", response_model=List[SessionOut])
async def list_sessions():
    rows = await db.sessions.find({}, _SESSION_PROJECTION).sort("updated_at", -1).to_list(200)
    return [SessionOut(**r) for r in rows]


@admin.get("/sessions/{sid}/traces")
async def get_traces(sid: str):
    rows = await db.turns.find({"session_id": sid}, {"_id": 0}) \
        .sort("created_at", 1).to_list(500)
    return {"turns": rows}


@admin.get("/langfuse/status")
async def langfuse_status():
    """Diagnostic endpoint: shows whether Langfuse auth succeeded and how many
    traces have been flushed since backend start. If auth_ok is false, your
    LANGFUSE_HOST likely doesn't match the cluster where you created the keys.
    """
    return LANGFUSE_STATUS


@admin.get("/guardrails/status")
async def guardrails_status():
    """Diagnostic endpoint: NeMo Guardrails state + counters (checks run / blocks)."""
    return GUARDRAILS_STATUS


app.include_router(api)
app.include_router(admin)

# CORS (issue L2): the browser spec forbids pairing credentials with a "*"
# origin. We authenticate with headers (no cookies), so credentials mode isn't
# needed for the open demo; only enable it when explicit origins are set.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
_cors_wildcard = (not _cors_origins) or ("*" in _cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_wildcard else _cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
