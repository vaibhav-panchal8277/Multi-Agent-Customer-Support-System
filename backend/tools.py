"""Mock REST-style tools for sub-agents (invoices, tickets, refund rules)."""
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
import uuid

# --- Mock invoices ---
# Issue dates are relative to "now" so the 30-day refund window is demonstrable
# regardless of the calendar date the demo is run (issue M1). INV-1001 is inside
# the window (refund-eligible); INV-1003 is outside it (shows the ineligible path).
_NOW = datetime.now(timezone.utc)


def _days_ago(n: int) -> str:
    return (_NOW - timedelta(days=n)).date().isoformat()


MOCK_INVOICES = {
    "INV-1001": {"customer": "acme@example.com", "amount": 79.00, "status": "paid",
                 "issued": _days_ago(10), "plan": "Pro Monthly"},
    "INV-1002": {"customer": "acme@example.com", "amount": 79.00, "status": "overdue",
                 "issued": _days_ago(45), "plan": "Pro Monthly"},
    "INV-1003": {"customer": "beta@example.com", "amount": 199.00, "status": "paid",
                 "issued": _days_ago(60), "plan": "Team Annual"},
}

MOCK_TICKETS = {
    "TCK-501": {"issue": "login loop after OAuth", "status": "open", "severity": "high"},
    "TCK-502": {"issue": "webhook 500 intermittent", "status": "in_progress", "severity": "med"},
}


def lookup_invoice(invoice_id: str) -> dict:
    inv = MOCK_INVOICES.get(invoice_id.upper())
    if not inv:
        return {"ok": False, "error": f"Invoice {invoice_id} not found"}
    return {"ok": True, "invoice_id": invoice_id.upper(), **inv}


def list_invoices_for(email: str) -> dict:
    matches = [
        {"invoice_id": k, **v} for k, v in MOCK_INVOICES.items()
        if v["customer"].lower() == email.lower()
    ]
    return {"ok": True, "count": len(matches), "invoices": matches}


def open_ticket(summary: str, severity: str = "med") -> dict:
    tid = f"TCK-{uuid.uuid4().hex[:8].upper()}"   # collision-free id (issue L4)
    MOCK_TICKETS[tid] = {"issue": summary, "status": "open", "severity": severity}
    return {"ok": True, "ticket_id": tid, "status": "open", "severity": severity}


def get_ticket(ticket_id: str) -> dict:
    t = MOCK_TICKETS.get(ticket_id.upper())
    if not t:
        return {"ok": False, "error": f"Ticket {ticket_id} not found"}
    return {"ok": True, "ticket_id": ticket_id.upper(), **t}


def check_refund_eligibility(invoice_id: str, reason: str) -> dict:
    """Rules engine: refunds allowed within 30 days for paid invoices, non-abuse reasons."""
    inv = MOCK_INVOICES.get(invoice_id.upper())
    if not inv:
        return {"ok": False, "eligible": False, "reason": "invoice_not_found"}
    if inv["status"] != "paid":
        return {"ok": True, "eligible": False, "reason": f"invoice_not_paid ({inv['status']})"}
    issued = datetime.fromisoformat(inv["issued"]).replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - issued).days
    if age_days > 30:
        return {"ok": True, "eligible": False, "reason": f"outside_30d_window ({age_days}d old)"}
    banned = ["fraud", "abuse", "chargeback"]
    if any(b in reason.lower() for b in banned):
        return {"ok": True, "eligible": False, "reason": "policy_violation"}
    return {"ok": True, "eligible": True, "invoice_id": invoice_id.upper(),
            "refund_amount": inv["amount"], "policy": "30-day money back"}


# Refunds already processed this process lifetime, keyed by invoice id, so the
# same invoice can't be refunded twice (issue M2). In-memory: fine for the mock.
_PROCESSED_REFUNDS: Dict[str, dict] = {}


def process_refund(invoice_id: str, amount: Optional[float] = None,
                   reason: str = "") -> dict:
    """Process a refund, re-validating server-side (issue M2).

    Hardening over the naive version:
      * Eligibility is re-checked here (defense in depth) — the caller/LLM
        saying "eligible" is never trusted.
      * The refund amount is derived from the invoice, not from the caller, so
        the model can't be talked into refunding an arbitrary sum. A supplied
        amount is accepted only if it matches the invoice exactly.
      * Duplicate refunds on the same invoice are rejected (idempotent).
    """
    inv_id = (invoice_id or "").upper()

    # Idempotency: never refund the same invoice twice.
    if inv_id in _PROCESSED_REFUNDS:
        prior = _PROCESSED_REFUNDS[inv_id]
        return {"ok": False, "error": "already_refunded", "invoice_id": inv_id,
                "refund_id": prior["refund_id"], "amount": prior["amount"],
                "processed_at": prior["processed_at"]}

    # Re-run the rules engine; only proceed if genuinely eligible.
    elig = check_refund_eligibility(inv_id, reason or "refund request")
    if not elig.get("eligible"):
        return {"ok": False, "error": "not_eligible",
                "reason": elig.get("reason"), "invoice_id": inv_id}

    server_amount = round(float(MOCK_INVOICES[inv_id]["amount"]), 2)
    if amount is not None and round(float(amount), 2) != server_amount:
        return {"ok": False, "error": "amount_mismatch", "invoice_id": inv_id,
                "expected_amount": server_amount, "requested_amount": amount}

    ref_id = f"REF-{uuid.uuid4().hex[:8].upper()}"   # collision-free (also L4)
    rec = {"ok": True, "refund_id": ref_id, "invoice_id": inv_id,
           "amount": server_amount,
           "processed_at": datetime.now(timezone.utc).isoformat()}
    _PROCESSED_REFUNDS[inv_id] = rec
    return dict(rec)


# Registry for supervisor's use
TOOL_REGISTRY = {
    "billing": {
        "lookup_invoice": lookup_invoice,
        "list_invoices_for": list_invoices_for,
    },
    "technical": {
        "open_ticket": open_ticket,
        "get_ticket": get_ticket,
    },
    "refunds": {
        "check_refund_eligibility": check_refund_eligibility,
        "process_refund": process_refund,
    },
}


# --- Authorization / scoping layer (issue C4) --------------------------------
# Tools that expose customer-owned data must be restricted to the session's
# bound customer identity, so that a user (or the LLM, if prompt-injected) cannot
# read or act on another customer's invoices/refunds. This is enforced
# server-side here — never by the LLM, which only supplies free-text args.

# Tools whose invoice_id argument must belong to the session's customer.
_OWNED_INVOICE_TOOLS = {"lookup_invoice", "check_refund_eligibility", "process_refund"}


def _invoice_owner(invoice_id: str) -> Optional[str]:
    inv = MOCK_INVOICES.get((invoice_id or "").upper())
    return inv["customer"].lower() if inv else None


def call_tool_scoped(agent_key: str, tool_name: str, args: dict,
                     customer_email: Optional[str] = None) -> dict:
    """Look up and invoke a tool with customer-scoping enforced.

    When ``customer_email`` is provided (the session's bound identity), tools
    that return customer-owned data are restricted to that customer:
      * ``list_invoices_for`` -> the email arg is FORCED to the session identity,
        ignoring any LLM-supplied email (prevents cross-customer enumeration).
      * ``lookup_invoice`` / ``check_refund_eligibility`` / ``process_refund`` ->
        denied unless the referenced invoice belongs to the customer.
    When ``customer_email`` is None (anonymous demo session), tools run
    unrestricted — bind a ``customer_email`` to the session to enable scoping.
    """
    fn = TOOL_REGISTRY.get(agent_key, {}).get(tool_name)
    if not fn:
        return {"ok": False, "error": f"unknown tool {tool_name}"}

    args = dict(args or {})
    scope = (customer_email or "").strip().lower() or None

    if scope is not None:
        # Enumeration guard: listing is always scoped to the caller's own email.
        if tool_name == "list_invoices_for":
            args["email"] = scope
        # Ownership guard for single-invoice tools.
        if tool_name in _OWNED_INVOICE_TOOLS:
            inv_id = args.get("invoice_id")
            if inv_id:
                owner = _invoice_owner(inv_id)
                # owner is None -> invoice doesn't exist; let the tool return its
                # own "not found" so the agent can ask the customer to re-check.
                if owner is not None and owner != scope:
                    return {"ok": False, "error": "not_authorized",
                            "detail": f"invoice {str(inv_id).upper()} does not "
                                      f"belong to this account"}

    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad args: {e}"}
