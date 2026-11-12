"""Routing tests for the LangGraph orchestrator with a scripted fake LLM."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import agents


class ScriptedLLM:
    """Replaces agents._llm_json / _llm_text. Each node pops its next scripted
    reply; once a script runs out its last reply repeats (so a buggy loop shows
    up as many calls, not as an IndexError)."""

    def __init__(self, scripts: Dict[str, List[Dict[str, Any]]], clarify_text="Could you clarify?"):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.clarify_text = clarify_text
        self.calls: List[str] = []
        self.prompts: Dict[str, List[str]] = {}

    async def llm_json(self, system, user, session_id, span_name):
        self.calls.append(span_name)
        self.prompts.setdefault(span_name, []).append(user)
        script = self.scripts[span_name]
        reply = script.pop(0) if len(script) > 1 else script[0]
        return {**reply, "_latency_ms": 1}

    async def llm_text(self, system, user, session_id):
        self.calls.append("clarify")
        self.prompts.setdefault("clarify", []).append(user)
        return {"text": self.clarify_text, "_latency_ms": 1}


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(agents, "_langfuse", None)

    def _run(scripts, message="help", customer_email=None):
        llm = ScriptedLLM(scripts)
        monkeypatch.setattr(agents, "_llm_json", llm.llm_json)
        monkeypatch.setattr(agents, "_llm_text", llm.llm_text)
        state = asyncio.run(agents.run_turn("s1", message, [], customer_email=customer_email))
        return state, llm

    return _run


def classify(intent, confidence=0.9):
    return {"intent": intent, "confidence": confidence, "reason": "test"}


FINAL = {"action": "final", "message": "All sorted."}


def test_happy_path_routes_to_specialist(run):
    state, llm = run({"supervisor": [classify("refunds")], "refunds": [FINAL]})
    assert state["response"] == "All sorted."
    assert state["current_agent_final"] == "refunds"
    assert llm.calls == ["supervisor", "refunds"]


def test_low_confidence_asks_clarifying_question(run):
    state, llm = run({"supervisor": [classify("billing", 0.3)]})
    assert state["current_agent_final"] == "clarify"
    assert llm.calls == ["supervisor", "clarify"]


def test_handoff_back_to_supervisor_does_not_loop(run):
    # billing says "not mine"; supervisor (same message) picks billing again
    state, llm = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "handoff", "to": "supervisor", "summary": "not a billing issue"}],
    })
    assert llm.calls.count("billing") == 1, llm.calls
    assert state["current_agent_final"] == "clarify"
    assert state["response"] == "Could you clarify?"


def test_specialist_ping_pong_is_stopped(run):
    state, llm = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "handoff", "to": "technical", "summary": "looks like a bug"}],
        "technical": [{"action": "handoff", "to": "billing", "summary": "actually billing"}],
    })
    assert llm.calls.count("billing") == 1 and llm.calls.count("technical") == 1, llm.calls
    assert state["current_agent_final"] == "clarify"


def test_tool_limit_gives_customer_facing_reply(run):
    state, llm = run({
        "supervisor": [classify("technical")],
        "technical": [{"action": "call_tool", "tool": "get_ticket",
                       "args": {"ticket_id": "TCK-501"}}],
    })
    assert llm.calls.count("technical") == agents.MAX_HOPS
    assert state["response"] == agents.FALLBACK_REPLY
    assert state["current_agent_final"] == "technical"
    assert "tool-call limit" not in state["response"]


def test_no_internal_handoff_summary_ever_reaches_customer(run):
    secret = "INTERNAL: customer seems angry, upsell Pro"
    for scripts in (
        {"supervisor": [classify("billing")],
         "billing": [{"action": "handoff", "to": "supervisor", "summary": secret}]},
        {"supervisor": [classify("billing")],
         "billing": [{"action": "handoff", "to": "technical", "summary": secret}],
         "technical": [{"action": "handoff", "to": "refunds", "summary": secret}],
         "refunds": [{"action": "handoff", "to": "billing", "summary": secret}]},
    ):
        state, _ = run(scripts)
        assert secret not in state["response"]
        assert "Handoff summary" not in state["response"]
        assert state.get("current_agent_final")


def test_every_ending_sets_final_agent(run):
    for scripts in (
        {"supervisor": [classify("general")]},
        {"supervisor": [classify("billing")], "billing": [FINAL]},
        {"supervisor": [classify("billing")],
         "billing": [{"action": "call_tool", "tool": "lookup_invoice",
                      "args": {"invoice_id": "INV-1001"}}]},
    ):
        state, _ = run(scripts)
        assert state.get("current_agent_final"), scripts


# ---------- hand-off summaries are forwarded ----------
NOTE = "customer reports error 502 when paying invoice INV-1001"


def test_handoff_summary_reaches_next_specialist(run):
    _, llm = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "handoff", "to": "technical", "summary": NOTE}],
        "technical": [FINAL],
    })
    assert NOTE in llm.prompts["technical"][0]
    assert "billing" in llm.prompts["technical"][0].lower()
    assert NOTE not in llm.prompts["billing"][0]


def test_handoff_summary_reaches_supervisor_and_clarify(run):
    _, llm = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "handoff", "to": "supervisor", "summary": NOTE}],
    })
    assert NOTE not in llm.prompts["supervisor"][0]
    assert NOTE in llm.prompts["supervisor"][1]
    assert NOTE in llm.prompts["clarify"][0]


def test_graph_scopes_tickets_to_the_session(run):
    state, _ = run({
        "supervisor": [classify("technical")],
        "technical": [{"action": "call_tool", "tool": "open_ticket", "args": {"summary": "bug"}},
                      {"action": "call_tool", "tool": "get_ticket", "args": {"ticket_id": "TCK-502"}},
                      FINAL],
    })
    opened, foreign = (tc["result"] for tc in state["tool_calls"])
    assert opened["ok"] is True
    import tools
    assert tools.MOCK_TICKETS[opened["ticket_id"]]["owner"] == "session:s1"
    assert foreign["error"] == "ticket_not_found"
