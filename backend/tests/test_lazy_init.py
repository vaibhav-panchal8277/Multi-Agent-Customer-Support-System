"""Importing the backend must not need secrets or touch the network; external
services are connected once, at server startup."""
from __future__ import annotations

import asyncio
import importlib

import pytest


class ExplodingLangfuse:
    def __init__(self, *a, **k):
        raise AssertionError("Langfuse must not be constructed at import time")


class FakeLangfuse:
    instances = 0

    def __init__(self, *a, **k):
        FakeLangfuse.instances += 1

    def auth_check(self):
        return True


@pytest.fixture
def fresh(monkeypatch):
    """Reload agents/guardrails_engine with no Groq key and a Langfuse that
    explodes if constructed; restore clean modules afterwards."""
    import langfuse
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr(langfuse, "Langfuse", ExplodingLangfuse)
    import agents
    import guardrails_engine
    agents = importlib.reload(agents)
    guardrails_engine = importlib.reload(guardrails_engine)
    yield agents, guardrails_engine
    monkeypatch.undo()
    importlib.reload(agents)
    importlib.reload(guardrails_engine)


def test_import_needs_no_key_and_no_network(fresh):
    agents, guardrails_engine = fresh
    assert agents._langfuse is None and agents._groq_client is None
    assert agents.LANGFUSE_STATUS["auth_ok"] is None
    assert guardrails_engine._rails is None and guardrails_engine._groq_client is None


def test_groq_client_errors_clearly_without_key(fresh):
    agents, guardrails_engine = fresh
    for mod in (agents, guardrails_engine):
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            mod._get_groq()


def test_groq_client_created_once(fresh, monkeypatch):
    agents, _ = fresh
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    assert agents._get_groq() is agents._get_groq()


def test_init_tracing_is_idempotent(fresh, monkeypatch):
    agents, _ = fresh
    monkeypatch.setattr(agents, "Langfuse", FakeLangfuse)
    FakeLangfuse.instances = 0
    agents.init_tracing()
    agents.init_tracing()
    assert FakeLangfuse.instances == 1
    assert agents.LANGFUSE_STATUS["enabled"] is True and agents._langfuse is not None


def test_init_tracing_without_keys_disables_cleanly(fresh, monkeypatch):
    agents, _ = fresh
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")
    agents.init_tracing()
    assert agents.LANGFUSE_STATUS["enabled"] is False
    assert "missing" in agents.LANGFUSE_STATUS["last_error"]


def test_uninitialised_guardrails_fail_closed(fresh):
    _, guardrails_engine = fresh
    if guardrails_engine.FAIL_MODE != "closed":
        pytest.skip("GUARDRAILS_FAIL_MODE=open in this environment")
    r = asyncio.run(guardrails_engine.check_input("hello"))
    assert r["blocked"] is True and r["unavailable"] is True


def test_server_startup_requires_groq_key(app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        with TestClient(app.server.app):
            pass


def test_server_startup_initialises_services(app, monkeypatch):
    from fastapi.testclient import TestClient
    called = []
    monkeypatch.setattr(app.server, "init_tracing", lambda: called.append("tracing"))
    monkeypatch.setattr(app.server, "init_guardrails", lambda: called.append("guardrails"))
    with TestClient(app.server.app):
        pass
    assert called == ["tracing", "guardrails"]
