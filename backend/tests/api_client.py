"""Shared helpers for the live-server (E2E) test suites.

`ApiClient` behaves like ``requests.Session`` but follows the API's auth model
automatically, so tests read like plain HTTP calls:

* remembers each session's key from ``POST /api/sessions`` and sends it as
  ``X-Session-Key`` on every request for that session (URL or JSON body);
* sends ``X-Admin-Token`` (env ``ADMIN_TOKEN``) only to admin-only endpoints, so
  per-session routes are exercised with the session key, not the admin bypass;
* sends ``Authorization: Bearer`` (env ``API_TOKEN``) when one is configured.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

# Demo identities from backend/tools.py MOCK_CUSTOMERS.
DEMO_PINS = {"acme@example.com": "4821", "beta@example.com": "7305"}

_SESSION_PATH = re.compile(r"/api/sessions/([^/]+)")


def _is_admin_path(method: str, path: str) -> bool:
    path = path.rstrip("/")
    return (
        (method == "GET" and path.endswith("/api/sessions"))
        or path.endswith("/traces")
        or path.endswith("/langfuse/status")
        or path.endswith("/guardrails/status")
    )


class ApiClient(requests.Session):
    def __init__(self, use_session_keys: bool = True) -> None:
        super().__init__()
        self.keys: Dict[str, str] = {}
        self.use_session_keys = use_session_keys
        self.headers.update({"Content-Type": "application/json"})
        if API_TOKEN:
            self.headers["Authorization"] = f"Bearer {API_TOKEN}"

    def _sid_for(self, path: str, body: Any) -> Optional[str]:
        m = _SESSION_PATH.search(path)
        if m and m.group(1) != "mine":
            return m.group(1)
        if isinstance(body, dict):
            return body.get("session_id")
        return None

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        method = method.upper()
        path = urlparse(url).path
        headers = dict(kwargs.pop("headers", None) or {})
        if ADMIN_TOKEN and _is_admin_path(method, path):
            headers.setdefault("X-Admin-Token", ADMIN_TOKEN)
        sid = self._sid_for(path, kwargs.get("json"))
        if self.use_session_keys and sid in self.keys:
            headers.setdefault("X-Session-Key", self.keys[sid])
        resp = super().request(method, url, *args, headers=headers, **kwargs)
        if method == "POST" and path.rstrip("/").endswith("/api/sessions") and resp.ok:
            data = resp.json()
            self.keys[data["id"]] = data["session_key"]
        return resp


def verify(client: ApiClient, api: str, sid: str, email: str = "acme@example.com") -> None:
    """Bind a demo customer identity to the session (needed for invoice/refund tools)."""
    r = client.post(f"{api}/sessions/{sid}/verify",
                    json={"email": email, "pin": DEMO_PINS[email]}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["customer_email"] == email
