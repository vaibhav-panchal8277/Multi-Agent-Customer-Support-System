"""End-to-end backend tests for Multi-Agent Support platform.

Tests exercise the real deployed backend (REACT_APP_BACKEND_URL) so that AI /
integration paths (Emergent LLM + Langfuse) are hit for real.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
CHAT_TIMEOUT = 90  # LLM calls can be slow


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    s.close()


@pytest.fixture(scope="module")
def created_session_ids():
    """Track sessions created so we can clean up."""
    ids = []
    yield ids
    # teardown
    s = requests.Session()
    for sid in ids:
        try:
            s.delete(f"{API}/sessions/{sid}", timeout=15)
        except Exception:
            pass


def _new_session(api_client, created_session_ids, title="TEST_session"):
    r = api_client.post(f"{API}/sessions", json={"title": title}, timeout=15)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    created_session_ids.append(sid)
    return sid


def _send(api_client, sid, msg):
    r = api_client.post(f"{API}/chat", json={"session_id": sid, "message": msg},
                        timeout=CHAT_TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- Health & agents ----------
class TestHealth:
    def test_root(self, api_client):
        r = api_client.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["service"] == "multi-agent-support"
        assert data["model"] == "gpt-5.4-mini"

    def test_agents_list(self, api_client):
        r = api_client.get(f"{API}/agents", timeout=10)
        assert r.status_code == 200
        agents = r.json()["agents"]
        keys = {a["key"] for a in agents}
        assert keys == {"supervisor", "billing", "technical", "refunds"}
        # colors are present
        for a in agents:
            assert isinstance(a.get("color"), str)
            assert a["color"].startswith("#")
            assert isinstance(a.get("name"), str)
            assert isinstance(a.get("scope"), str)


# ---------- Session CRUD ----------
class TestSessions:
    def test_create_and_list(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_create")
        r = api_client.get(f"{API}/sessions", timeout=10)
        assert r.status_code == 200
        sessions = r.json()
        assert any(s["id"] == sid for s in sessions)
        # ordered by updated_at desc
        ups = [s["updated_at"] for s in sessions]
        assert ups == sorted(ups, reverse=True)

    def test_get_session_empty(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_empty")
        r = api_client.get(f"{API}/sessions/{sid}", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["session"]["id"] == sid
        assert body["messages"] == []

    def test_get_session_404(self, api_client):
        r = api_client.get(f"{API}/sessions/nonexistent-{uuid.uuid4()}", timeout=10)
        assert r.status_code == 404

    def test_resolve(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_resolve")
        r = api_client.post(f"{API}/sessions/{sid}/resolve", timeout=10)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        g = api_client.get(f"{API}/sessions/{sid}", timeout=10).json()
        assert g["session"]["status"] == "resolved"

    def test_resolve_404(self, api_client):
        r = api_client.post(f"{API}/sessions/nope-{uuid.uuid4()}/resolve", timeout=10)
        assert r.status_code == 404

    def test_delete(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_del")
        r = api_client.delete(f"{API}/sessions/{sid}", timeout=10)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        g = api_client.get(f"{API}/sessions/{sid}", timeout=10)
        assert g.status_code == 404


# ---------- Chat / multi-agent behavior (real LLM) ----------
class TestChatBilling:
    def test_billing_query_routes_to_billing_and_calls_tool(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_billing")
        res = _send(api_client, sid,
                    "I was double-charged for invoice INV-1002")
        assert res["session_id"] == sid
        assert res["agent"] == "billing", f"expected billing, got {res['agent']}. traces={res.get('trace_events')}"
        assert res.get("intent") == "billing"
        assert isinstance(res.get("response"), str) and len(res["response"]) > 0
        # confidence numeric
        assert isinstance(res.get("confidence"), (int, float))
        # tool call includes lookup_invoice
        tool_names = [tc["tool"] for tc in res.get("tool_calls", [])]
        assert "lookup_invoice" in tool_names, f"tool_calls={res.get('tool_calls')}"
        # trace_events must include a supervisor classify + a billing tool_call event
        types = [ev.get("type") for ev in res.get("trace_events", [])]
        assert "classify" in types
        assert "tool_call" in types

    def test_multi_turn_billing_list_invoices(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_billing_multi")
        _send(api_client, sid, "I was double-charged for invoice INV-1002")
        follow = _send(api_client, sid,
                       "Can you list all my invoices for acme@example.com?")
        assert follow["agent"] == "billing", f"expected billing on follow-up, got {follow['agent']}"
        tool_names = [tc["tool"] for tc in follow.get("tool_calls", [])]
        assert "list_invoices_for" in tool_names, f"tool_calls={follow.get('tool_calls')}"


class TestChatTechnical:
    def test_technical_query(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_tech")
        res = _send(api_client, sid,
                    "Our OAuth login redirects in a loop and users cannot sign in.")
        assert res["agent"] == "technical", f"got {res['agent']}"
        assert res.get("intent") == "technical"
        assert len(res["response"]) > 0


class TestChatRefunds:
    def test_refund_query_calls_eligibility(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_refund")
        res = _send(api_client, sid,
                    "Please refund INV-1001, I cancelled last week.")
        assert res["agent"] == "refunds", f"got {res['agent']}"
        tool_names = [tc["tool"] for tc in res.get("tool_calls", [])]
        assert "check_refund_eligibility" in tool_names, f"tool_calls={res.get('tool_calls')}"


class TestChatClarify:
    def test_ambiguous_asks_clarifying_question(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_clarify")
        res = _send(api_client, sid, "Hey, can you help me?")
        # agent should be clarify (supervisor asks clarifying question path)
        assert res["agent"] in ("clarify", "supervisor"), f"got {res['agent']}"
        assert len(res["response"]) > 0
        # intent likely 'general' with low confidence
        assert res.get("intent") in ("general", "billing", "technical", "refunds", None)


class TestChatPersistenceAndTraces:
    def test_messages_and_traces_persisted(self, api_client, created_session_ids):
        sid = _new_session(api_client, created_session_ids, "TEST_persist")
        res = _send(api_client, sid, "I was double-charged for invoice INV-1002")

        # session get returns messages
        g = api_client.get(f"{API}/sessions/{sid}", timeout=10).json()
        msgs = g["messages"]
        # 1 user + 1 assistant
        assert len(msgs) == 2
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant"]
        asst = msgs[1]
        assert asst.get("agent") in ("billing", "technical", "refunds",
                                     "supervisor", "clarify")
        assert isinstance(asst.get("trace_events", []), list)
        assert isinstance(asst.get("tool_calls", []), list)

        # traces endpoint returns turns
        t = api_client.get(f"{API}/sessions/{sid}/traces", timeout=10).json()
        assert "turns" in t and len(t["turns"]) >= 1
        turn = t["turns"][0]
        assert turn["session_id"] == sid
        assert isinstance(turn.get("trace_events"), list)
        assert turn.get("user_message") == "I was double-charged for invoice INV-1002"

        # sanity: session summary updated
        sess = api_client.get(f"{API}/sessions", timeout=10).json()
        me = next((s for s in sess if s["id"] == sid), None)
        assert me is not None
        assert me["message_count"] >= 2
        assert me["last_agent"] is not None

        # response echoes latency in trace events (LLM was actually called)
        latencies = [ev.get("latency_ms") for ev in res.get("trace_events", []) if ev.get("latency_ms")]
        assert any(isinstance(x, int) and x > 0 for x in latencies), \
            f"no positive latency in trace_events: {res.get('trace_events')}"

    def test_chat_missing_session_returns_404(self, api_client):
        r = api_client.post(f"{API}/chat", json={
            "session_id": f"missing-{uuid.uuid4()}",
            "message": "hi",
        }, timeout=15)
        assert r.status_code == 404


class TestChatHandoff:
    def test_mixed_query_may_handoff(self, api_client, created_session_ids):
        """Mixed query might produce a handoff event. Non-strict - we just ensure
        the endpoint responds and traces are recorded."""
        sid = _new_session(api_client, created_session_ids, "TEST_handoff")
        res = _send(api_client, sid,
                    "Please refund INV-1001, but also the app is crashing on login.")
        assert res["agent"] in ("refunds", "technical", "billing", "supervisor", "clarify"), \
            f"got {res['agent']}"
        types = [ev.get("type") for ev in res.get("trace_events", [])]
        # Must at least have classify
        assert "classify" in types
