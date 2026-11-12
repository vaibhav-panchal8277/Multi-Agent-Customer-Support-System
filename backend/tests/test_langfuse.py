"""Langfuse v4 SDK integration tests.

Verifies the bug fix:
- /api/langfuse/status must show enabled=true, auth_ok=true, last_error=null
- traces_sent must increment after each POST /api/chat
- last_flush_ts must be a fresh unix timestamp (recent)
- Core /api/chat response schema must remain intact (Langfuse must not break it)
"""
from __future__ import annotations

import os
import time

import pytest

from tests.api_client import ADMIN_TOKEN, ApiClient, verify

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
# Live-server E2E suite: skipped unless REACT_APP_BACKEND_URL points at a running backend.
pytestmark = pytest.mark.skipif(not BASE_URL, reason="set REACT_APP_BACKEND_URL to run E2E tests")
API = f"{BASE_URL}/api"
CHAT_TIMEOUT = 90


@pytest.fixture(scope="module")
def api_client():
    if not ADMIN_TOKEN:
        pytest.skip("Langfuse status is admin-only: set ADMIN_TOKEN to run these tests")
    s = ApiClient()
    yield s
    s.close()


@pytest.fixture(scope="module")
def cleanup_sids(api_client):
    ids = []
    yield ids
    for sid in ids:
        try:
            api_client.delete(f"{API}/sessions/{sid}", timeout=15)
        except Exception:
            pass


def _new_session(api_client, cleanup_sids, title="TEST_lf"):
    r = api_client.post(f"{API}/sessions", json={"title": title}, timeout=15)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    cleanup_sids.append(sid)
    return sid


def _status(api_client, wait_for_flush_after=None):
    """Fetch status. Flushing happens in the background after the response, so
    optionally poll (up to 10s) until a flush newer than the given time lands."""
    deadline = time.time() + 10
    while True:
        r = api_client.get(f"{API}/langfuse/status", timeout=10)
        assert r.status_code == 200, r.text
        st = r.json()
        if (wait_for_flush_after is None or time.time() > deadline
                or (st.get("last_flush_ts") or 0) >= wait_for_flush_after):
            return st
        time.sleep(0.2)


# ---------- Langfuse status endpoint (pre-chat) ----------
class TestLangfuseStatus:
    def test_status_endpoint_shape_and_boot_auth(self, api_client):
        st = _status(api_client)
        # Shape
        for key in (
            "enabled", "host", "public_key_prefix",
            "auth_ok", "last_error", "last_flush_ts", "traces_sent",
        ):
            assert key in st, f"missing key {key} in {st}"

        # Boot-time auth check should have succeeded with the keys in .env
        assert st["enabled"] is True, f"Langfuse not enabled: {st}"
        assert st["auth_ok"] is True, f"auth_check failed: {st}"
        assert st["last_error"] is None, f"unexpected last_error: {st}"

        # Host + key prefix reflect .env
        assert st["host"] == os.environ.get(
            "LANGFUSE_HOST", "https://us.cloud.langfuse.com"
        ) or st["host"].startswith("https://"), st
        assert isinstance(st["public_key_prefix"], str) and st["public_key_prefix"], st
        assert st["public_key_prefix"].startswith("pk-lf-"), st

        # traces_sent should be a non-negative int
        assert isinstance(st["traces_sent"], int)
        assert st["traces_sent"] >= 0


# ---------- Chat -> Langfuse traces_sent must increment ----------
class TestLangfuseTracesIncrement:
    def test_single_chat_increments_traces_sent(self, api_client, cleanup_sids):
        before = _status(api_client)
        sid = _new_session(api_client, cleanup_sids, "TEST_lf_single")

        sent_at = time.time()
        r = api_client.post(f"{API}/chat", json={
            "session_id": sid,
            "message": "I was double-charged for invoice INV-1002",
        }, timeout=CHAT_TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()

        # Response schema must still be intact
        for k in ("session_id", "agent", "response", "trace_events", "tool_calls"):
            assert k in body, f"missing {k} in chat response {body}"
        assert isinstance(body["trace_events"], list)
        assert isinstance(body["tool_calls"], list)

        after = _status(api_client, wait_for_flush_after=sent_at)
        assert after["last_error"] is None, f"Langfuse errored after chat: {after}"
        assert after["traces_sent"] >= before["traces_sent"] + 1, (
            f"traces_sent did not increment. before={before}, after={after}"
        )
        # last_flush_ts should be fresh (within last 5 min)
        assert isinstance(after["last_flush_ts"], (int, float))
        assert after["last_flush_ts"] > time.time() - 300, (
            f"last_flush_ts is stale: {after['last_flush_ts']} vs now={time.time()}"
        )
        # auth still ok
        assert after["auth_ok"] is True

    def test_multiple_chats_increment_each_time(self, api_client, cleanup_sids):
        before = _status(api_client)
        sid = _new_session(api_client, cleanup_sids, "TEST_lf_multi")

        messages = [
            "Please help me understand invoice INV-1001.",
            "Refund INV-1001 please, I cancelled.",
            "My integration is failing with a 500 error, help!",
        ]
        counters = [before["traces_sent"]]
        last_flushes = [before.get("last_flush_ts") or 0]
        for m in messages:
            sent_at = time.time()
            r = api_client.post(f"{API}/chat", json={"session_id": sid, "message": m},
                                timeout=CHAT_TIMEOUT)
            assert r.status_code == 200, r.text
            st = _status(api_client, wait_for_flush_after=sent_at)
            assert st["last_error"] is None, f"error mid-flight: {st}"
            counters.append(st["traces_sent"])
            last_flushes.append(st["last_flush_ts"] or 0)

        # each successive counter must be > previous
        for i in range(1, len(counters)):
            assert counters[i] > counters[i - 1], (
                f"traces_sent did not strictly increase: {counters}"
            )
        # each flush ts must be > previous (monotonic)
        for i in range(1, len(last_flushes)):
            assert last_flushes[i] >= last_flushes[i - 1], (
                f"last_flush_ts regressed: {last_flushes}"
            )


# ---------- Core chat behavior must remain intact ----------
class TestChatUnaffectedByLangfuse:
    def test_refund_query_still_routes_and_calls_tool(self, api_client, cleanup_sids):
        sid = _new_session(api_client, cleanup_sids, "TEST_lf_refund")
        verify(api_client, API, sid, "acme@example.com")
        r = api_client.post(f"{API}/chat", json={
            "session_id": sid,
            "message": "Please refund INV-1001, I cancelled last week.",
        }, timeout=CHAT_TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()

        # Full response schema
        for k in ("session_id", "agent", "intent", "confidence",
                  "trace_events", "tool_calls", "response"):
            assert k in body, f"chat response missing {k}: {body}"

        assert body["agent"] == "refunds", f"expected refunds agent, got {body['agent']}"
        assert body["intent"] == "refunds"
        tool_names = [tc.get("tool") for tc in body.get("tool_calls", [])]
        assert "check_refund_eligibility" in tool_names, (
            f"expected check_refund_eligibility tool_call, got {body.get('tool_calls')}"
        )

        # And Langfuse status still clean
        st = _status(api_client)
        assert st["last_error"] is None
        assert st["auth_ok"] is True


# ---------- Error surfacing sanity (introspective) ----------
class TestLangfuseErrorSurfacing:
    """We can't safely mutate live .env from the test, but we can assert the
    contract: after a fresh successful boot + chats, last_error MUST be None
    (i.e. the v4 API rename was actually applied and no silent exception is
    happening). If the old v3 method names (`start_as_current_span`,
    `update_trace`) were still in the code, we'd observe last_error being set
    after the first chat because the try/except now records the exception.
    """
    def test_no_last_error_after_first_chat(self, api_client, cleanup_sids):
        sid = _new_session(api_client, cleanup_sids, "TEST_lf_err")
        r = api_client.post(f"{API}/chat", json={
            "session_id": sid,
            "message": "hello",
        }, timeout=CHAT_TIMEOUT)
        assert r.status_code == 200, r.text
        st = _status(api_client)
        assert st["last_error"] is None, (
            f"Langfuse instrumentation raised (v4 API mismatch?): {st['last_error']}"
        )
        assert st["traces_sent"] >= 1
