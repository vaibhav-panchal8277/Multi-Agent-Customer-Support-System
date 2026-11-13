"""Offline unit tests for tool authorization (no server, LLM or env needed).

Run:  cd backend && pytest tests/test_tools_scoping.py
"""
from __future__ import annotations

import pytest

import tools
from tools import (TOOL_REGISTRY, call_tool_scoped, execute_confirmed_refund,
                   verify_customer)

ACME = "acme@example.com"
BETA = "beta@example.com"


@pytest.fixture(autouse=True)
def _reset_refunds():
    tools._PROCESSED_REFUNDS.clear()
    yield
    tools._PROCESSED_REFUNDS.clear()


# ---- fail closed without a verified identity ----
@pytest.mark.parametrize("agent,tool,args", [
    ("billing", "lookup_invoice", {"invoice_id": "INV-1001"}),
    ("billing", "list_invoices_for", {"email": ACME}),
    ("refunds", "check_refund_eligibility", {"invoice_id": "INV-1001", "reason": "x"}),
    ("refunds", "request_refund", {"invoice_id": "INV-1001"}),
])
@pytest.mark.parametrize("identity", [None, "", "   "])
def test_customer_data_tools_require_identity(agent, tool, args, identity):
    r = call_tool_scoped(agent, tool, args, identity)
    assert r == {"ok": False, "error": "identity_not_verified", "detail": r["detail"]}


# ---- tickets: owned by the verified customer, or by the session that opened them ----
def test_anonymous_session_can_open_and_read_its_own_ticket():
    r = call_tool_scoped("technical", "open_ticket", {"summary": "login loop"}, None, "sess-1")
    assert r["ok"] is True
    tid = r["ticket_id"]
    got = call_tool_scoped("technical", "get_ticket", {"ticket_id": tid}, None, "sess-1")
    assert got["ok"] is True and got["issue"] == "login loop"
    assert "owner" not in got  # ownership is internal


def test_other_sessions_and_customers_cannot_read_a_ticket():
    tid = call_tool_scoped("technical", "open_ticket", {"summary": "x"}, None, "sess-1")["ticket_id"]
    for email, sid in ((None, "sess-2"), (ACME, "sess-3"), (None, None)):
        r = call_tool_scoped("technical", "get_ticket", {"ticket_id": tid}, email, sid)
        assert r == {"ok": False, "error": "ticket_not_found",
                     "detail": f"No ticket {tid} on this account"}, (email, sid)


def test_seeded_tickets_belong_to_their_customers():
    assert call_tool_scoped("technical", "get_ticket", {"ticket_id": "tck-501"}, ACME, "s")["ok"]
    assert call_tool_scoped("technical", "get_ticket", {"ticket_id": "TCK-501"}, BETA, "s")[
        "error"] == "ticket_not_found"


def test_verified_customer_owns_tickets_across_sessions():
    tid = call_tool_scoped("technical", "open_ticket", {"summary": "x"}, ACME, "sess-1")["ticket_id"]
    assert call_tool_scoped("technical", "get_ticket", {"ticket_id": tid}, ACME, "sess-9")["ok"]


def test_llm_cannot_choose_ticket_owner_or_bogus_severity():
    r = call_tool_scoped("technical", "open_ticket",
                         {"summary": "x" * 5000, "severity": "CRITICAL!!", "owner": BETA},
                         None, "sess-1")
    assert r["ok"] and r["severity"] == "med"
    assert call_tool_scoped("technical", "get_ticket", {"ticket_id": r["ticket_id"]},
                            BETA, "other")["error"] == "ticket_not_found"
    assert len(tools.MOCK_TICKETS[r["ticket_id"]]["issue"]) <= 500


def test_open_ticket_needs_a_session_or_identity():
    r = call_tool_scoped("technical", "open_ticket", {"summary": "x"}, None, None)
    assert r["ok"] is False


# ---- ownership ----
def test_own_invoice_visible():
    r = call_tool_scoped("billing", "lookup_invoice", {"invoice_id": "inv-1001"}, ACME)
    assert r["ok"] is True and r["customer"] == ACME


def test_other_customers_invoice_looks_like_not_found():
    foreign = call_tool_scoped("billing", "lookup_invoice", {"invoice_id": "INV-1003"}, ACME)
    missing = call_tool_scoped("billing", "lookup_invoice", {"invoice_id": "INV-9999"}, ACME)
    assert foreign["error"] == missing["error"] == "invoice_not_found"
    assert "199" not in str(foreign)  # no amount leaked


def test_list_is_forced_to_session_identity():
    r = call_tool_scoped("billing", "list_invoices_for", {"email": BETA}, ACME)
    assert {i["customer"] for i in r["invoices"]} == {ACME}


# ---- malformed LLM arguments never crash ----
@pytest.mark.parametrize("args", ["INV-1001", ["INV-1001"], 5, {"invoice_id": 1001},
                                  {"invoice_id": None}, {"invoice_id": "INV-1001", "x": 1}, {}])
def test_bad_args_return_errors(args):
    r = call_tool_scoped("billing", "lookup_invoice", args, ACME)
    assert r["ok"] is False and r["error"].startswith("bad args")


def test_unknown_tool():
    assert call_tool_scoped("billing", "process_refund", {"invoice_id": "INV-1001"}, ACME)["error"] \
        == "unknown tool process_refund"


# ---- refunds need explicit confirmation ----
def test_llm_cannot_execute_refunds():
    assert "process_refund" not in TOOL_REGISTRY["refunds"]


def test_request_refund_only_proposes():
    r = call_tool_scoped("refunds", "request_refund", {"invoice_id": "INV-1001"}, ACME)
    assert r["ok"] is True and r["status"] == "pending_confirmation"
    assert r["confirmation_id"].startswith("RFC-") and r["amount"] == 79.0
    assert not tools._PROCESSED_REFUNDS  # nothing moved


def test_request_refund_ineligible():
    r = call_tool_scoped("refunds", "request_refund", {"invoice_id": "INV-1002"}, ACME)
    assert r["ok"] is False and r["error"] == "not_eligible"


def test_confirmed_refund_executes_once_for_owner_only():
    assert execute_confirmed_refund("INV-1001", None)["error"] == "identity_not_verified"
    assert execute_confirmed_refund("INV-1001", BETA)["error"] == "invoice_not_found"
    first = execute_confirmed_refund("INV-1001", ACME)
    assert first["ok"] is True and first["amount"] == 79.0
    second = execute_confirmed_refund("INV-1001", ACME)
    assert second["ok"] is False and second["error"] == "already_refunded"
    after = call_tool_scoped("refunds", "request_refund", {"invoice_id": "INV-1001"}, ACME)
    assert after["error"] == "already_refunded"


# ---- identity verification ----
def test_verify_customer():
    assert verify_customer(" ACME@example.com ", "4821") == ACME
    assert verify_customer(ACME, "7305") is None          # someone else's PIN
    assert verify_customer("nobody@example.com", "4821") is None
    assert verify_customer(ACME, "") is None
    assert verify_customer(ACME, "४८२१") is None           # non-ASCII must not raise


# ---------- used_args reports what actually ran ----------
def test_used_args_reflect_server_overrides():
    used = {}
    call_tool_scoped("billing", "list_invoices_for", {"email": BETA}, ACME, "s1", used_args=used)
    assert used == {"email": ACME}
    used = {}
    call_tool_scoped("technical", "open_ticket", {"summary": "x", "owner": "evil"}, None, "s9",
                     used_args=used)
    assert used == {"summary": "x", "owner": "session:s9"}


def test_internal_args_are_not_offered_to_the_llm():
    from agents import _tool_arg_names
    assert "owner" in tools.INTERNAL_ARGS
    assert _tool_arg_names("technical", "open_ticket") == ["summary", "severity"]
