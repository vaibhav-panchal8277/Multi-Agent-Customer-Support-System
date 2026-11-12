"""Input guardrail runs in parallel with the graph; tools wait for its verdict."""
from __future__ import annotations

import json
import time

import agents
import tools
from tests.conftest import FakeGuard, new_session


def _chat(app, sid, h, msg="my webhook returns 500"):
    return app.client.post("/api/chat", json={"session_id": sid, "message": msg}, headers=h)


def test_rail_and_graph_overlap(app):
    app.guard.input_delay = 0.4
    app.graph.delay = 0.4
    sid, h = new_session(app.client)
    started = time.monotonic()
    assert _chat(app, sid, h).status_code == 200
    assert time.monotonic() - started < 0.7, "input rail and graph ran sequentially"


def test_blocked_rail_cancels_graph(app):
    app.guard.input_verdict = dict(FakeGuard.BLOCK)
    app.guard.input_delay = 0.1
    app.graph.delay = 5
    sid, h = new_session(app.client)
    started = time.monotonic()
    body = _chat(app, sid, h).json()
    assert body["agent"] == "guardrail"
    assert time.monotonic() - started < 2, "blocked turn waited for the graph"
    assert app.graph.cancelled


class TicketLLM:
    """Technical agent opens a ticket, then answers."""

    def __init__(self, log):
        self.log = log

    async def __call__(self, system, user, *, json_mode=False, temperature=0.4):
        if system.startswith("You are the Supervisor Agent of"):
            return json.dumps({"intent": "technical", "confidence": 0.95}), None
        if "tool open_ticket" in user:
            return json.dumps({"action": "final", "message": "Ticket opened."}), None
        return json.dumps({"action": "call_tool", "tool": "open_ticket",
                           "args": {"summary": "webhook 500"}}), None


def _real_graph(app, monkeypatch, log):
    monkeypatch.setattr(app.server, "run_turn", agents.run_turn)
    monkeypatch.setattr(agents, "_langfuse", None)
    monkeypatch.setattr(agents, "_groq_chat", TicketLLM(log))
    real_open = tools.open_ticket

    def spy_open(**kw):
        log.append(("tool", time.monotonic()))
        return real_open(**kw)

    monkeypatch.setitem(tools.TOOL_REGISTRY["technical"], "open_ticket", spy_open)

    real_check = app.guard.check_input

    async def timed_check(text):
        r = await real_check(text)
        log.append(("rail_done", time.monotonic()))
        return r

    monkeypatch.setattr(app.server, "check_input", timed_check)


def test_tool_waits_for_rail_verdict(app, monkeypatch):
    log = []
    _real_graph(app, monkeypatch, log)
    app.guard.input_delay = 0.3
    sid, h = new_session(app.client)
    body = _chat(app, sid, h).json()
    assert body["response"] == "Ticket opened."
    kinds = [k for k, _ in log]
    assert kinds == ["rail_done", "tool"], log


def test_no_tool_side_effect_when_rail_blocks(app, monkeypatch):
    log = []
    _real_graph(app, monkeypatch, log)
    app.guard.input_delay = 0.3
    app.guard.input_verdict = dict(FakeGuard.BLOCK)
    before = dict(tools.MOCK_TICKETS)
    sid, h = new_session(app.client)
    assert _chat(app, sid, h).json()["agent"] == "guardrail"
    assert ("tool" not in [k for k, _ in log]) and tools.MOCK_TICKETS == before
