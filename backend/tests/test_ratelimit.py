"""Rate limiting + resolved-session behaviour."""
from __future__ import annotations

from ratelimit import SlidingWindowLimiter
from tests.conftest import new_session


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------- limiter unit ----------
def test_limiter_allows_up_to_limit_then_blocks_until_window_passes():
    clock = Clock()
    lim = SlidingWindowLimiter(3, 60, clock=clock)
    assert [lim.hit("a") for _ in range(3)] == [0, 0, 0]
    assert 59 < lim.hit("a") <= 60
    assert lim.hit("b") == 0  # keys are independent
    clock.t += 30
    assert 29 < lim.hit("a") <= 30
    clock.t += 30.001
    assert lim.hit("a") == 0


def test_rejected_hits_do_not_extend_the_window():
    clock = Clock()
    lim = SlidingWindowLimiter(1, 10, clock=clock)
    lim.hit("a")
    for _ in range(5):
        clock.t += 1
        lim.hit("a")
    clock.t += 5.001  # 10s after the first (accepted) hit
    assert lim.hit("a") == 0


def test_disabled_limiter():
    lim = SlidingWindowLimiter(0, 60)
    assert all(lim.hit("a") == 0 for _ in range(100))


def test_idle_keys_are_pruned():
    clock = Clock()
    lim = SlidingWindowLimiter(1, 10, clock=clock)
    lim.MAX_KEYS = 5
    for i in range(5):
        lim.hit(f"k{i}")
    clock.t += 11
    lim.hit("new")
    assert set(lim._hits) == {"new"}


# ---------- API ----------
def _chat(app, sid, h):
    return app.client.post("/api/chat", json={"session_id": sid, "message": "hi"}, headers=h)


def test_chat_is_rate_limited_per_session(app, monkeypatch):
    monkeypatch.setattr(app.server, "_chat_limiter", SlidingWindowLimiter(2, 60))
    sid, h = new_session(app.client)
    assert _chat(app, sid, h).status_code == 200
    assert _chat(app, sid, h).status_code == 200
    r = _chat(app, sid, h)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert len(app.graph.calls) == 2  # limited before any LLM work
    other, oh = new_session(app.client)
    assert _chat(app, other, oh).status_code == 200


def test_session_creation_is_rate_limited_per_client(app, monkeypatch):
    monkeypatch.setattr(app.server, "_session_limiter", SlidingWindowLimiter(2, 60))
    c = app.client
    assert c.post("/api/sessions", json={}).status_code == 200
    assert c.post("/api/sessions", json={}).status_code == 200
    r = c.post("/api/sessions", json={})
    assert r.status_code == 429 and "Retry-After" in r.headers


# ---------- resolved sessions ----------
def test_resolved_session_rejects_chat_until_reopened(app):
    c = app.client
    sid, h = new_session(c)
    assert c.post(f"/api/sessions/{sid}/resolve", headers=h).status_code == 200
    r = _chat(app, sid, h)
    assert r.status_code == 409 and not app.graph.calls
    assert c.post(f"/api/sessions/{sid}/reopen", headers=h).json()["status"] == "open"
    assert _chat(app, sid, h).status_code == 200


def test_reopen_requires_session_key(app):
    c = app.client
    sid, h = new_session(c)
    c.post(f"/api/sessions/{sid}/resolve", headers=h)
    assert c.post(f"/api/sessions/{sid}/reopen").status_code == 403
