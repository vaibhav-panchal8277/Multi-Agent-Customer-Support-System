"""Multi-agent orchestrator: Supervisor + Billing/Technical/Refunds sub-agents.

Uses LangGraph's StateGraph for explicit state + conditional routing, with each
node calling GPT-5.4-mini via the emergentintegrations LlmChat client. Every
agent action, tool call, and hand-off is wrapped in a Langfuse span for tracing.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict

from emergentintegrations.llm.chat import LlmChat, UserMessage
from langfuse import Langfuse
from langgraph.graph import END, StateGraph

from tools import TOOL_REGISTRY

# --- Config ---
EMERGENT_LLM_KEY = os.environ["EMERGENT_LLM_KEY"]
MODEL_NAME = "gpt-5.4-mini"
MODEL_PROVIDER = "openai"

# Langfuse client (auto-reads env vars). Safe if keys are set.
_langfuse: Optional[Langfuse] = None
try:
    _langfuse = Langfuse(
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        host=os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com"),
    )
except Exception:  # keys missing / network down: keep app running
    _langfuse = None


# --- Graph state ---
class ChatState(TypedDict, total=False):
    session_id: str
    user_message: str
    history: List[Dict[str, str]]           # [{role, content, agent?}]
    current_agent: Literal["supervisor", "billing", "technical", "refunds", "clarify", "done"]
    intent: str
    confidence: float
    handoff_reason: Optional[str]
    response: str
    tool_calls: List[Dict[str, Any]]
    trace_events: List[Dict[str, Any]]      # per-turn timeline for UI
    hops: int
    current_agent_final: str


AGENT_COLORS = {
    "supervisor": "#FFFFFF",
    "billing": "#10b981",
    "technical": "#0ea5e9",
    "refunds": "#f59e0b",
    "clarify": "#a78bfa",
}


# ---------- LLM helper ----------
async def _with_retries(coro_factory, *, tries: int = 3, base_delay: float = 0.8):
    import asyncio
    last_exc = None
    for i in range(tries):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            if "rate" in msg or "429" in msg or "concurrent" in msg or "timeout" in msg:
                await asyncio.sleep(base_delay * (2 ** i))
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def _llm_json(system: str, user: str, session_id: str, span_name: str) -> Dict[str, Any]:
    """Call the LLM and try to parse JSON out of the response."""
    started = time.time()

    async def _call():
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=session_id,
            system_message=system,
        ).with_model(MODEL_PROVIDER, MODEL_NAME)
        return await chat.send_message(UserMessage(text=user))

    raw = await _with_retries(_call)
    latency_ms = int((time.time() - started) * 1000)

    # Parse first JSON object we can find
    parsed: Dict[str, Any] = {}
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = {"_raw": raw}
        else:
            parsed = {"_raw": raw}

    parsed["_latency_ms"] = latency_ms
    parsed["_raw_snippet"] = raw[:400]
    return parsed


async def _llm_text(system: str, user: str, session_id: str) -> Dict[str, Any]:
    started = time.time()

    async def _call():
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=session_id,
            system_message=system,
        ).with_model(MODEL_PROVIDER, MODEL_NAME)
        return await chat.send_message(UserMessage(text=user))

    text = await _with_retries(_call)
    return {"text": text, "_latency_ms": int((time.time() - started) * 1000)}


def _record(state: ChatState, event: Dict[str, Any]) -> None:
    state.setdefault("trace_events", []).append(
        {**event, "ts": time.time(), "color": AGENT_COLORS.get(event.get("agent", ""), "#A1A1AA")}
    )


# ---------- Nodes ----------
SUPERVISOR_SYSTEM = """You are the Supervisor Agent of a customer-support platform.
Classify the customer's most recent message into ONE of: billing, technical, refunds, general.
Return strict JSON: {"intent":"billing|technical|refunds|general","confidence":0.0-1.0,"reason":"<brief>"}.
- billing = invoices, charges, subscription plan, payment methods
- technical = bugs, errors, integrations, outages, product not working
- refunds = requesting money back, cancel + refund, dispute charge
- general = greetings/small talk/unclear
Be strict. If unsure, lower confidence."""


async def supervisor_node(state: ChatState) -> ChatState:
    hist = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-6:])
    prompt = f"Conversation so far:\n{hist}\n\nLatest customer message: {state['user_message']}"
    result = await _llm_json(SUPERVISOR_SYSTEM, prompt, state["session_id"], "supervisor")

    intent = str(result.get("intent", "general")).lower()
    confidence = float(result.get("confidence", 0.5) or 0.5)
    reason = str(result.get("reason", ""))

    state["intent"] = intent
    state["confidence"] = confidence
    _record(state, {
        "agent": "supervisor",
        "type": "classify",
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "latency_ms": result.get("_latency_ms"),
        "label": f"Classified as {intent} ({int(confidence*100)}%)",
    })

    # Route
    if confidence < 0.55 or intent == "general":
        state["current_agent"] = "clarify"
    elif intent in ("billing", "technical", "refunds"):
        state["current_agent"] = intent
        _record(state, {
            "agent": "supervisor",
            "type": "handoff",
            "to": intent,
            "label": f"Handing off to {intent.title()} Agent",
        })
    else:
        state["current_agent"] = "clarify"
    return state


CLARIFY_SYSTEM = """You are the Supervisor Agent asking a single, short clarifying question
to figure out whether the user's issue is about billing, technical support, or a refund.
Be warm and one sentence max. Do not answer the question yourself."""


async def clarify_node(state: ChatState) -> ChatState:
    r = await _llm_text(CLARIFY_SYSTEM,
                        f"Customer said: {state['user_message']}",
                        state["session_id"])
    state["response"] = r["text"].strip()
    state["current_agent"] = "supervisor"  # UI badge
    _record(state, {
        "agent": "supervisor",
        "type": "clarify",
        "label": "Confidence low → asking clarifying question",
        "latency_ms": r["_latency_ms"],
    })
    state["current_agent"] = "done"
    state["current_agent_final"] = "clarify"  # for UI
    return state


def _make_subagent_node(agent_key: str, system_prompt: str, tool_names: List[str]):
    """Returns a graph node function for a specialised sub-agent."""

    tool_desc = "\n".join(
        f'- {name}({", ".join(_tool_arg_names(agent_key, name))})' for name in tool_names
    )
    full_system = f"""{system_prompt}

You have these tools available (mock APIs):
{tool_desc}

Respond with STRICT JSON of one of these shapes:
1) {{"action":"call_tool","tool":"<name>","args":{{...}}}}  -> call a tool first
2) {{"action":"final","message":"<reply to customer>"}}     -> answer the customer
3) {{"action":"handoff","to":"billing|technical|refunds|supervisor","summary":"<why>"}} -> hand back to Supervisor if the query is outside your scope.
Never invent invoice IDs; if unknown, ask the customer or use list_invoices_for(email).
"""

    async def node(state: ChatState) -> ChatState:
        hist = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-6:])
        # Include prior tool call receipts in this turn
        tool_receipts = "\n".join(
            f"tool {tc['tool']} -> {json.dumps(tc['result'])[:400]}"
            for tc in state.get("tool_calls", [])
        )
        prompt = (f"Conversation so far:\n{hist}\n\n"
                  f"Latest customer message: {state['user_message']}\n\n"
                  f"Tool receipts so far this turn:\n{tool_receipts or '(none)'}")

        result = await _llm_json(full_system, prompt, state["session_id"], agent_key)
        action = str(result.get("action", "final")).lower()
        _record(state, {
            "agent": agent_key,
            "type": "reason",
            "action": action,
            "latency_ms": result.get("_latency_ms"),
            "label": f"{agent_key.title()} Agent reasoning ({action})",
        })

        if action == "call_tool":
            tool_name = result.get("tool")
            args = result.get("args") or {}
            tool_fn = TOOL_REGISTRY.get(agent_key, {}).get(tool_name)
            if not tool_fn:
                res = {"ok": False, "error": f"unknown tool {tool_name}"}
            else:
                try:
                    res = tool_fn(**args)
                except TypeError as e:
                    res = {"ok": False, "error": f"bad args: {e}"}
            state.setdefault("tool_calls", []).append(
                {"agent": agent_key, "tool": tool_name, "args": args, "result": res}
            )
            _record(state, {
                "agent": agent_key,
                "type": "tool_call",
                "tool": tool_name,
                "args": args,
                "result": res,
                "label": f"Tool: {tool_name}({', '.join(f'{k}={v}' for k,v in args.items())})",
            })
            # loop back to same agent to synthesize response
            state["current_agent"] = agent_key
            state["hops"] = state.get("hops", 0) + 1
            if state["hops"] >= 4:
                state["response"] = "I've hit my tool-call limit. Let me connect you back to the Supervisor."
                state["current_agent"] = "done"
            return state

        if action == "handoff":
            to = str(result.get("to", "supervisor")).lower()
            summary = result.get("summary", "")
            state["handoff_reason"] = summary
            _record(state, {
                "agent": agent_key,
                "type": "handoff",
                "to": to,
                "summary": summary,
                "label": f"{agent_key.title()} → {to.title()}: {summary[:80]}",
            })
            if to in ("billing", "technical", "refunds"):
                state["current_agent"] = to
                state["hops"] = state.get("hops", 0) + 1
                if state["hops"] >= 4:
                    state["current_agent"] = "done"
                    state["response"] = f"Handoff summary: {summary}"
                return state
            # back to supervisor
            state["current_agent"] = "supervisor"
            state["hops"] = state.get("hops", 0) + 1
            if state["hops"] >= 4:
                state["current_agent"] = "done"
                state["response"] = f"Handoff summary: {summary}"
            return state

        # final
        state["response"] = str(result.get("message", "")).strip() or "How else can I help?"
        state["current_agent_final"] = agent_key
        state["current_agent"] = "done"
        _record(state, {
            "agent": agent_key,
            "type": "final",
            "label": f"{agent_key.title()} Agent responded",
        })
        return state

    return node


def _tool_arg_names(agent: str, tool: str) -> List[str]:
    fn = TOOL_REGISTRY.get(agent, {}).get(tool)
    if not fn:
        return []
    import inspect
    return [p for p in inspect.signature(fn).parameters]


BILLING_SYSTEM = """You are the Billing Agent. Handle invoices, charges, subscription plans.
If the customer describes a technical bug, hand off to technical. If they clearly ask for a refund, hand off to refunds."""

TECHNICAL_SYSTEM = """You are the Technical Support Agent. Diagnose product bugs, integration errors, outages.
You may open a ticket on the customer's behalf. If the issue is actually about billing or a refund, hand off."""

REFUNDS_SYSTEM = """You are the Refunds Agent. Determine eligibility via the rules engine, then process the refund if eligible.
If the issue is not really about money back (e.g. it's a bug), hand off."""


billing_node = _make_subagent_node("billing", BILLING_SYSTEM,
                                   ["lookup_invoice", "list_invoices_for"])
technical_node = _make_subagent_node("technical", TECHNICAL_SYSTEM,
                                     ["open_ticket", "get_ticket"])
refunds_node = _make_subagent_node("refunds", REFUNDS_SYSTEM,
                                   ["check_refund_eligibility", "process_refund"])


# ---------- Router ----------
def route_from_supervisor(state: ChatState) -> str:
    return state.get("current_agent", "clarify")


def route_after_agent(state: ChatState) -> str:
    ca = state.get("current_agent", "done")
    if ca == "done":
        return END
    return ca


# ---------- Build graph ----------
def build_graph():
    g = StateGraph(ChatState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("clarify", clarify_node)
    g.add_node("billing", billing_node)
    g.add_node("technical", technical_node)
    g.add_node("refunds", refunds_node)

    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route_from_supervisor,
                            {"billing": "billing", "technical": "technical",
                             "refunds": "refunds", "clarify": "clarify",
                             "done": END})
    for agent in ("billing", "technical", "refunds"):
        g.add_conditional_edges(agent, route_after_agent,
                                {"billing": "billing", "technical": "technical",
                                 "refunds": "refunds", "supervisor": "supervisor",
                                 END: END})
    g.add_edge("clarify", END)
    return g.compile()


_GRAPH = build_graph()


async def run_turn(session_id: str, user_message: str,
                   history: List[Dict[str, str]]) -> ChatState:
    """Run a single turn through the multi-agent graph, with Langfuse tracing."""
    initial: ChatState = {
        "session_id": session_id,
        "user_message": user_message,
        "history": history,
        "current_agent": "supervisor",
        "tool_calls": [],
        "trace_events": [],
        "hops": 0,
    }

    # Wrap the whole turn in a Langfuse trace
    if _langfuse is not None:
        try:
            with _langfuse.start_as_current_span(
                name="support_turn",
                input={"user_message": user_message, "session_id": session_id},
            ) as span:
                final = await _GRAPH.ainvoke(initial, config={"recursion_limit": 20})
                span.update_trace(
                    session_id=session_id,
                    output={
                        "agent": final.get("current_agent_final") or final.get("current_agent"),
                        "response": final.get("response", ""),
                    },
                    metadata={
                        "intent": final.get("intent"),
                        "confidence": final.get("confidence"),
                        "tool_calls": final.get("tool_calls", []),
                    },
                )
                return final
        except Exception:
            pass  # fall through to plain run
    return await _GRAPH.ainvoke(initial, config={"recursion_limit": 20})


def flush_langfuse() -> None:
    if _langfuse is not None:
        try:
            _langfuse.flush()
        except Exception:
            pass
