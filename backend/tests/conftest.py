"""Offline test harness: the real FastAPI app on an in-memory Mongo, with the
LLM graph and the guardrails replaced by controllable fakes.

No network, Groq key, Langfuse account or MongoDB server is needed.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "mesh_test")

ADMIN = "test-admin-token"


class FakeGuard:
    """Stands in for guardrails_engine.check_input / check_output."""

    PASS = {"allowed": True, "blocked": False, "rail": None, "reason": "passed", "latency_ms": 1}
    BLOCK = {"allowed": False, "blocked": True, "rail": "self check", "reason": "blocked",
             "latency_ms": 1}

    def __init__(self) -> None:
        self.input_verdict: Dict[str, Any] = dict(self.PASS)
        self.output_verdict: Dict[str, Any] = dict(self.PASS)
        self.calls: List[tuple] = []
        self.input_delay = 0.0

    async def check_input(self, text: str) -> Dict[str, Any]:
        self.calls.append(("input", text))
        if self.input_delay:
            await asyncio.sleep(self.input_delay)
        return dict(self.input_verdict)

    async def check_output(self, user_text: str, bot_text: str) -> Dict[str, Any]:
        self.calls.append(("output", bot_text))
        return dict(self.output_verdict)


class FakeGraph:
    """Stands in for agents.run_turn; records what the graph was given."""

    def __init__(self) -> None:
        self.state: Dict[str, Any] = {
            "response": "Here is your answer.", "current_agent_final": "billing",
            "intent": "billing", "confidence": 0.9, "tool_calls": [],
            "trace_events": [], "hops": 0,
        }
        self.calls: List[Dict[str, Any]] = []
        self.raise_exc: Optional[BaseException] = None
        self.delay = 0.0
        self.cancelled = False

    async def run_turn(self, session_id, user_message, history, customer_email=None, **kw):
        self.calls.append({"session_id": session_id, "message": user_message,
                           "history": list(history), "customer_email": customer_email, **kw})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.state)


@pytest.fixture
def app(monkeypatch):
    """Reload server.py under test env and yield a TestClient + fakes."""
    from mongomock_motor import AsyncMongoMockClient
    import dotenv
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # ignore any local .env
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    for var in ("API_TOKEN", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)

    import server
    server = importlib.reload(server)
    guard, graph = FakeGuard(), FakeGraph()
    monkeypatch.setattr(server, "db", AsyncMongoMockClient()["mesh_test"])
    monkeypatch.setattr(server, "check_input", guard.check_input)
    monkeypatch.setattr(server, "check_output", guard.check_output)
    monkeypatch.setattr(server, "run_turn", graph.run_turn)
    # rate limits off by default; test_ratelimit.py installs strict ones
    from ratelimit import SlidingWindowLimiter
    monkeypatch.setattr(server, "_chat_limiter", SlidingWindowLimiter(0, 60))
    monkeypatch.setattr(server, "_session_limiter", SlidingWindowLimiter(0, 60))
    for hook in ("init_tracing", "init_guardrails"):
        if hasattr(server, hook):
            monkeypatch.setattr(server, hook, lambda: None)

    with TestClient(server.app) as client:
        yield SimpleNamespace(client=client, server=server, guard=guard, graph=graph)


def new_session(client, title: str = "t") -> tuple:
    r = client.post("/api/sessions", json={"title": title})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], {"X-Session-Key": body["session_key"]}


def admin_headers() -> Dict[str, str]:
    return {"X-Admin-Token": ADMIN}
