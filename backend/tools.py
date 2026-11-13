"""Mock REST-style tools for sub-agents (invoices, tickets, refund rules)."""
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
import secrets
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

# "owner" is the verified customer email, or "session:<id>" for a ticket opened
# by an unverified session. It is internal and never returned to the LLM.
MOCK_TICKETS = {
    "TCK-501": {"issue": "login loop after OAuth", "status": "open", "severity": "high",
                "owner": "acme@example.com"},
    "TCK-502": {"issue": "webhook 500 intermittent", "status": "in_progress", "severity": "med",
                "owner": "beta@example.com"},
}
_SEVERITIES = {"low", "med", "high"}


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


def open_ticket(summary: str, severity: str = "med", owner: str = "") -> dict:
    """Open a ticket. ``owner`` is set by call_tool_scoped, never by the LLM."""
    if not owner:
        return {"ok": False, "error": "no session to own the ticket"}
    severity = str(severity).lower() if str(severity).lower() in _SEVERITIES else "med"
    tid = f"TCK-{uuid.uuid4().hex[:8].upper()}"   # collision-free id (issue L4)
    MOCK_TICKETS[tid] = {"issue": str(summary)[:500], "status": "open",
                         "severity": severity, "owner": owner}
    return {"ok": True, "ticket_id": tid, "status": "open", "severity": severity}


def get_ticket(ticket_id: str) -> dict:
    t = MOCK_TICKETS.get(ticket_id.upper())
    if not t:
        return {"ok": False, "error": f"Ticket {ticket_id} not found"}
    public = {k: v for k, v in t.items() if k != "owner"}
    return {"ok": True, "ticket_id": ticket_id.upper(), **public}


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

    Not exposed to the LLM: only reached via :func:`execute_confirmed_refund`
    after the customer explicitly confirms in the UI.

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


def request_refund(invoice_id: str, reason: str = "") -> dict:
    """LLM-facing refund tool: validates and *proposes* a refund, never executes it.

    The model can be talked into calling a tool, so it is never allowed to move
    money on its own. This returns a ``pending_confirmation`` receipt with a
    ``confirmation_id``; the server stores it against the session and the
    customer must press "Confirm refund" in the UI (``POST
    /api/sessions/{sid}/refunds/{confirmation_id}/confirm``), which is what
    finally calls :func:`execute_confirmed_refund`.
    """
    inv_id = (invoice_id or "").upper()
    if inv_id in _PROCESSED_REFUNDS:
        return {"ok": False, "error": "already_refunded", "invoice_id": inv_id,
                "refund_id": _PROCESSED_REFUNDS[inv_id]["refund_id"]}
    elig = check_refund_eligibility(inv_id, reason or "refund request")
    if not elig.get("eligible"):
        return {"ok": False, "error": "not_eligible",
                "reason": elig.get("reason"), "invoice_id": inv_id}
    return {
        "ok": True,
        "status": "pending_confirmation",
        "confirmation_id": f"RFC-{uuid.uuid4().hex[:12].upper()}",
        "invoice_id": inv_id,
        "amount": round(float(MOCK_INVOICES[inv_id]["amount"]), 2),
        "note": ("Refund NOT processed yet. Tell the customer to press the "
                 "'Confirm refund' button to authorize it."),
    }


# --- Mock customer directory (identity verification) ------------------------
# Stand-in for a real identity provider / login. A session only gets access to
# customer-owned data after the customer proves who they are with email + PIN
# (POST /api/sessions/{sid}/verify). These are DEMO values, like MOCK_INVOICES.
MOCK_CUSTOMERS = {
    "acme@example.com": {"name": "Acme Inc.", "pin": "4821"},
    "beta@example.com": {"name": "Beta LLC", "pin": "7305"},
}


def verify_customer(email: str, pin: str) -> Optional[str]:
    """Return the normalised email if email+PIN match the directory, else None."""
    e = (email or "").strip().lower()
    rec = MOCK_CUSTOMERS.get(e)
    expected = rec["pin"] if rec else "\x00no-such-customer"
    # compare as bytes in constant time (str compare_digest rejects non-ASCII)
    ok = secrets.compare_digest(str(pin or "").strip().encode(), expected.encode())
    return e if (rec is not None and ok) else None


# Registry of tools each sub-agent (i.e. the LLM) may call. Note that
# process_refund is deliberately NOT here: only the customer's explicit
# confirmation (server endpoint) can execute a refund.
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
        "request_refund": request_refund,
    },
}


# --- Authorization / scoping layer -------------------------------------------
# Tools that expose customer-owned data are restricted to the session's
# *verified* customer identity. This is enforced server-side here, never by the
# LLM, which only supplies free-text args. It FAILS CLOSED: a session with no
# verified identity cannot read or act on any customer's invoices or refunds.

# Arguments only the server sets (call_tool_scoped overwrites them). They are
# hidden from the tool list shown to the LLM and from the activity feed.
INTERNAL_ARGS = frozenset({"owner"})

# Tools whose invoice_id argument must belong to the session's customer.
_OWNED_INVOICE_TOOLS = {"lookup_invoice", "check_refund_eligibility", "request_refund"}
# All tools that touch customer-owned data.
_CUSTOMER_DATA_TOOLS = _OWNED_INVOICE_TOOLS | {"list_invoices_for"}


def _invoice_owner(invoice_id: str) -> Optional[str]:
    inv = MOCK_INVOICES.get((invoice_id or "").upper())
    return inv["customer"].lower() if inv else None


def _not_on_account(invoice_id: str) -> dict:
    # Same answer for "doesn't exist" and "belongs to someone else", so the
    # tool can't be used as an oracle to discover other customers' invoice ids.
    return {"ok": False, "error": "invoice_not_found",
            "detail": f"No invoice {str(invoice_id).upper()} on this account"}


def _ticket_owners(customer_email: Optional[str], session_id: Optional[str]) -> set:
    owners = set()
    if customer_email:
        owners.add(customer_email)
    if session_id:
        owners.add(f"session:{session_id}")
    return owners


def call_tool_scoped(agent_key: str, tool_name: str, args: Any,
                     customer_email: Optional[str] = None,
                     session_id: Optional[str] = None,
                     used_args: Optional[dict] = None) -> dict:
    """Look up and invoke a tool with customer-scoping enforced.

    * Customer-data tools require a verified ``customer_email`` (the session's
      bound identity); without one they return ``identity_not_verified``.
    * ``list_invoices_for`` -> the email arg is FORCED to the session identity.
    * single-invoice tools -> the invoice must belong to the session identity.
    * tickets -> owned by the verified customer, or by the session that opened
      them; ``open_ticket``'s owner is forced, ``get_ticket`` checks it.
    * Malformed LLM output (non-object args, non-string ids, unexpected kwargs)
      returns a ``bad args`` error instead of crashing the turn.

    If ``used_args`` is given, it is filled with the arguments actually used
    (after the server's overrides), so callers can log what really ran rather
    than what the LLM asked for.
    """
    fn = TOOL_REGISTRY.get(agent_key, {}).get(tool_name)
    if not fn:
        return {"ok": False, "error": f"unknown tool {tool_name}"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "bad args: expected a JSON object"}
    args = dict(args)
    if used_args is not None:
        used_args.clear()
        used_args.update(args)

    if tool_name in _CUSTOMER_DATA_TOOLS:
        scope = (customer_email or "").strip().lower() or None
        if scope is None:
            return {"ok": False, "error": "identity_not_verified",
                    "detail": ("The customer has not verified their account. Ask them "
                               "to press 'Verify account' and sign in with their email "
                               "and PIN before looking up invoices or refunds.")}
        if tool_name == "list_invoices_for":
            args["email"] = scope
        else:
            inv_id = args.get("invoice_id")
            if not isinstance(inv_id, str) or not inv_id.strip():
                return {"ok": False, "error": "bad args: invoice_id must be a non-empty string"}
            args["invoice_id"] = inv_id.strip()
            if _invoice_owner(args["invoice_id"]) != scope:
                return _not_on_account(args["invoice_id"])

    if tool_name in ("open_ticket", "get_ticket"):
        scope = (customer_email or "").strip().lower() or None
        owners = _ticket_owners(scope, session_id)
        if tool_name == "open_ticket":
            args["owner"] = scope or (f"session:{session_id}" if session_id else "")
        else:
            tid = args.get("ticket_id")
            if not isinstance(tid, str) or not tid.strip():
                return {"ok": False, "error": "bad args: ticket_id must be a non-empty string"}
            args["ticket_id"] = tid.strip()
            ticket = MOCK_TICKETS.get(args["ticket_id"].upper())
            if ticket is None or ticket.get("owner") not in owners:
                return {"ok": False, "error": "ticket_not_found",
                        "detail": f"No ticket {args['ticket_id'].upper()} on this account"}

    if used_args is not None:
        used_args.clear()
        used_args.update(args)
    try:
        return fn(**args)
    except (TypeError, ValueError, AttributeError) as e:
        return {"ok": False, "error": f"bad args: {e}"}


def execute_confirmed_refund(invoice_id: str, customer_email: Optional[str]) -> dict:
    """Execute a refund the customer explicitly confirmed (server-side only).

    Re-checks ownership against the session's verified identity at execution
    time, then defers to :func:`process_refund` (eligibility, server-derived
    amount, idempotency).
    """
    scope = (customer_email or "").strip().lower() or None
    if scope is None:
        return {"ok": False, "error": "identity_not_verified"}
    if _invoice_owner(invoice_id) != scope:
        return _not_on_account(invoice_id)
    return process_refund(invoice_id, reason="customer confirmed refund")
