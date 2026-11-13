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
        self.systems: Dict[str, List[str]] = {}

    async def llm_json(self, system, user, session_id, span_name):
        self.calls.append(span_name)
        self.prompts.setdefault(span_name, []).append(user)
        self.systems.setdefault(span_name, []).append(system)
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

    def _run(scripts, message="help", customer_email=None, history=None):
        llm = ScriptedLLM(scripts)
        monkeypatch.setattr(agents, "_llm_json", llm.llm_json)
        monkeypatch.setattr(agents, "_llm_text", llm.llm_text)
        state = asyncio.run(agents.run_turn("s1", message, history or [],
                                            customer_email=customer_email))
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


# ---------- current account status is stated every turn ----------
# Regression (live smoke test on Render): after the customer verified, the
# Billing agent saw its own earlier "you're not verified" reply in the history
# and answered from it without calling the tool.
STALE_HISTORY = [
    {"role": "user", "content": "What is the amount on invoice INV-1001?"},
    {"role": "assistant", "content": "I can't view invoice details because your account "
                                     "isn't verified yet. Please press Verify account."},
]


def test_verified_status_is_stated_above_stale_history(run):
    _, llm = run({"supervisor": [classify("billing")], "billing": [FINAL]},
                 message="What is the amount on invoice INV-1001?",
                 customer_email="acme@example.com", history=STALE_HISTORY)
    prompt = llm.prompts["billing"][0]
    assert "VERIFIED as acme@example.com" in prompt
    assert "NOT VERIFIED" not in prompt
    # stated before the conversation, so the stale reply can't be read first
    assert prompt.index("Account status") < prompt.index("isn't verified yet")


def test_unverified_status_is_stated(run):
    _, llm = run({"supervisor": [classify("billing")], "billing": [FINAL]})
    assert "NOT VERIFIED" in llm.prompts["billing"][0]


def test_every_specialist_gets_status_and_the_retry_rule(run):
    for agent in ("billing", "technical", "refunds"):
        _, llm = run({"supervisor": [classify(agent)], agent: [FINAL]},
                     customer_email="acme@example.com")
        assert "VERIFIED as acme@example.com" in llm.prompts[agent][0], agent
        system = llm.systems[agent][0]
        assert "overrides anything" in system and "call your tools again" in system, agent
        assert "do not retry it" not in system, agent  # the old, over-broad rule


def test_status_follows_identity_within_the_same_session(run):
    """Same session, before and after verifying: the prompt must change."""
    _, before = run({"supervisor": [classify("billing")], "billing": [FINAL]},
                    customer_email=None, history=STALE_HISTORY)
    _, after = run({"supervisor": [classify("billing")], "billing": [FINAL]},
                   customer_email="acme@example.com", history=STALE_HISTORY)
    assert "NOT VERIFIED" in before.prompts["billing"][0]
    assert "VERIFIED as acme@example.com" in after.prompts["billing"][0]


# ---------- tool args: server-set fields hidden, effective args logged ----------
# Live finding: the Technical agent was shown open_ticket(summary, severity, owner),
# filled owner="technical", and the activity feed showed that value even though
# the server overwrote it.
def test_tool_list_hides_server_set_owner(run):
    _, llm = run({"supervisor": [classify("technical")], "technical": [FINAL]})
    tool_lines = [ln for ln in llm.systems["technical"][0].splitlines()
                  if ln.startswith("- open_ticket")]
    assert tool_lines == ["- open_ticket(summary, severity)"]


def test_trace_shows_the_args_that_actually_ran(run):
    import tools
    state, _ = run({
        "supervisor": [classify("technical")],
        "technical": [{"action": "call_tool", "tool": "open_ticket",
                       "args": {"summary": "login loop", "owner": "technical"}},
                      FINAL],
    })
    call = state["tool_calls"][0]
    assert call["args"] == {"summary": "login loop"}
    label = next(e["label"] for e in state["trace_events"] if e["type"] == "tool_call")
    assert label == "Tool: open_ticket(summary=login loop)"
    assert tools.MOCK_TICKETS[call["result"]["ticket_id"]]["owner"] == "session:s1"


def test_forced_email_is_what_gets_logged(run):
    state, _ = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "call_tool", "tool": "list_invoices_for",
                     "args": {"email": "beta@example.com"}}, FINAL],
    }, customer_email="acme@example.com")
    call = state["tool_calls"][0]
    assert call["args"] == {"email": "acme@example.com"}
    assert {i["customer"] for i in call["result"]["invoices"]} == {"acme@example.com"}


def test_rejected_call_still_logs_what_the_agent_asked(run):
    state, _ = run({
        "supervisor": [classify("billing")],
        "billing": [{"action": "call_tool", "tool": "lookup_invoice", "args": "INV-1001"}, FINAL],
    })
    assert state["tool_calls"][0]["args"] == "INV-1001"
    assert state["tool_calls"][0]["result"]["error"].startswith("bad args")
