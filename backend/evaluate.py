"""Evaluation harness for the multi-agent support platform.

Runs a labeled set of customer messages through the graph and reports:
  * routing accuracy    (Supervisor picked the right sub-agent)
  * tool-call precision (right tool used on cases where a tool is expected)
  * latency p50/p95     (per turn, real Emergent LLM calls)
  * clarify fallback    (low-confidence -> clarifying question)

Run:
  cd /app/backend
  python evaluate.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from agents import run_turn  # noqa: E402


# --- Labeled eval set -------------------------------------------------------
# Each case: message, expected agent, and optionally expected tool.
# Chosen to cover the same intent surface a support desk sees on day one.
EVAL_CASES: List[Dict[str, Any]] = [
    # ---- Billing (invoices / charges / subscription) ----
    {"msg": "I was double-charged for invoice INV-1002", "agent": "billing", "tool": "lookup_invoice"},
    {"msg": "Can you show me all invoices for acme@example.com?", "agent": "billing", "tool": "list_invoices_for"},
    {"msg": "Why did my card get charged twice this month?", "agent": "billing"},
    {"msg": "What is my current plan? I signed up for Pro.", "agent": "billing"},
    {"msg": "The subscription cost seems higher than last month", "agent": "billing"},
    {"msg": "Look up invoice INV-1001 please", "agent": "billing", "tool": "lookup_invoice"},

    # ---- Technical (bugs / errors / integrations) ----
    {"msg": "The OAuth login keeps redirecting in a loop", "agent": "technical"},
    {"msg": "Our webhook is returning 500 intermittently", "agent": "technical"},
    {"msg": "I can't upload files, the button just spins forever", "agent": "technical"},
    {"msg": "API endpoint /users returns 401 even with a valid token", "agent": "technical"},
    {"msg": "The dashboard is completely blank after login", "agent": "technical"},
    {"msg": "Please open a ticket - our integration broke this morning", "agent": "technical", "tool": "open_ticket"},

    # ---- Refunds (money back / eligibility) ----
    {"msg": "Please refund INV-1001, I cancelled last week", "agent": "refunds", "tool": "check_refund_eligibility"},
    {"msg": "I want my money back for INV-1002", "agent": "refunds", "tool": "check_refund_eligibility"},
    {"msg": "Can I get a refund for the last payment?", "agent": "refunds"},
    {"msg": "Cancel my subscription and refund the last charge", "agent": "refunds"},
    {"msg": "Requesting refund for INV-1003 - the service didn't work", "agent": "refunds", "tool": "check_refund_eligibility"},

    # ---- General / clarify (should trigger low-confidence fallback) ----
    {"msg": "Hi there", "agent": "clarify"},
    {"msg": "Can you help me?", "agent": "clarify"},
    {"msg": "Hello, quick question", "agent": "clarify"},
]


async def _run_case(case: Dict[str, Any]) -> Dict[str, Any]:
    sid = f"eval-{uuid.uuid4()}"
    t0 = time.perf_counter()
    state = await run_turn(sid, case["msg"], history=[])
    latency_ms = int((time.perf_counter() - t0) * 1000)

    predicted_agent = state.get("current_agent_final") or "supervisor"
    tool_calls = state.get("tool_calls", [])
    tool_used: Optional[str] = tool_calls[0]["tool"] if tool_calls else None
    return {
        "msg": case["msg"],
        "expected_agent": case["agent"],
        "predicted_agent": predicted_agent,
        "agent_ok": predicted_agent == case["agent"],
        "expected_tool": case.get("tool"),
        "tool_used": tool_used,
        "tool_ok": (case.get("tool") is None) or (tool_used == case.get("tool")),
        "intent": state.get("intent"),
        "confidence": state.get("confidence"),
        "latency_ms": latency_ms,
    }


async def main() -> None:
    print(f"Running {len(EVAL_CASES)} cases through the multi-agent graph...\n")
    results: List[Dict[str, Any]] = []
    for i, case in enumerate(EVAL_CASES, 1):
        r = await _run_case(case)
        results.append(r)
        mark = "✓" if r["agent_ok"] else "✗"
        tool_mark = ""
        if r["expected_tool"]:
            tool_mark = " · tool " + ("✓" if r["tool_ok"] else f"✗ (got {r['tool_used']})")
        print(f"[{i:02d}] {mark} {r['predicted_agent']:<10} "
              f"conf={r['confidence']:.2f}  {r['latency_ms']:>5}ms  "
              f"{r['msg'][:60]}{tool_mark}")

    # --- Aggregate metrics ---
    total = len(results)
    routing_ok = sum(r["agent_ok"] for r in results)
    tool_cases = [r for r in results if r["expected_tool"]]
    tool_ok = sum(r["tool_ok"] for r in tool_cases)
    latencies = [r["latency_ms"] for r in results]
    confidences = [r["confidence"] for r in results if r["confidence"] is not None]

    def pct(x: int, n: int) -> str:
        return f"{(x / n * 100):.1f}%" if n else "n/a"

    print("\n" + "=" * 68)
    print(" RESULTS")
    print("=" * 68)
    print(f" Total cases                : {total}")
    print(f" Routing accuracy           : {routing_ok}/{total}  ({pct(routing_ok, total)})")
    print(f" Tool-call precision        : {tool_ok}/{len(tool_cases)}  ({pct(tool_ok, len(tool_cases))})")
    print(f" Mean supervisor confidence : {statistics.mean(confidences):.2f}")
    print(f" Latency p50 / p95 / max    : "
          f"{statistics.median(latencies):.0f}ms / "
          f"{sorted(latencies)[int(0.95*len(latencies))-1]:.0f}ms / "
          f"{max(latencies)}ms")
    print(f" Mean latency               : {statistics.mean(latencies):.0f}ms")
    print("=" * 68)

    # Failures
    misses = [r for r in results if not r["agent_ok"]]
    if misses:
        print("\n Misclassifications:")
        for r in misses:
            print(f"  expected={r['expected_agent']:<9} got={r['predicted_agent']:<10} "
                  f"conf={r['confidence']:.2f}  msg='{r['msg']}'")


if __name__ == "__main__":
    asyncio.run(main())
