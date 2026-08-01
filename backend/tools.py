"""Mock REST-style tools for sub-agents (invoices, tickets, refund rules)."""
from datetime import datetime, timezone, timedelta
import random

# --- Mock invoices ---
MOCK_INVOICES = {
    "INV-1001": {"customer": "acme@example.com", "amount": 79.00, "status": "paid",
                 "issued": "2026-01-05", "plan": "Pro Monthly"},
    "INV-1002": {"customer": "acme@example.com", "amount": 79.00, "status": "overdue",
                 "issued": "2026-02-05", "plan": "Pro Monthly"},
    "INV-1003": {"customer": "beta@example.com", "amount": 199.00, "status": "paid",
                 "issued": "2026-01-15", "plan": "Team Annual"},
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
    tid = f"TCK-{random.randint(600, 999)}"
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


def process_refund(invoice_id: str, amount: float) -> dict:
    ref_id = f"REF-{random.randint(1000, 9999)}"
    return {"ok": True, "refund_id": ref_id, "invoice_id": invoice_id.upper(),
            "amount": amount, "processed_at": datetime.now(timezone.utc).isoformat()}


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
