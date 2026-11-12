"""NeMo Guardrails integration for MESH Support.

Wraps Groq's openai/gpt-oss-120b as a LangChain-compatible model
and feeds it into a NeMo `LLMRails` engine that runs LLM-based self-check rails:
  * input rail  -> blocks jailbreak / prompt-injection / unsafe / off-topic messages
  * output rail -> moderates the assistant's final answer before it reaches the user

Exposes async `check_input` / `check_output` helpers used by server.py.

Failure handling: transient Groq errors (rate limits, timeouts, 5xx) are retried
with backoff. If a rail still can't run, the default is FAIL CLOSED (the message
or reply is treated as blocked), because a safety filter that silently switches
off under load or abuse is worse than a brief outage — and the agents use the
same Groq API, so they would be failing at the same time anyway. Set
GUARDRAILS_FAIL_MODE=open to allow on error (local dev only). Every failure is
recorded in GUARDRAILS_STATUS for observability.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from langchain_core.language_models.llms import LLM

from groq import AsyncGroq, Groq

log = logging.getLogger("guardrails")

GUARD_MODEL = os.environ.get("GUARDRAILS_MODEL",
                             os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))

# "closed" (default): block when a rail can't run. "open": allow (dev only).
FAIL_MODE = "open" if os.environ.get("GUARDRAILS_FAIL_MODE", "closed").strip().lower() == "open" \
    else "closed"
_RETRIES = 3
_BASE_DELAY_S = 0.8


def _is_transient(e: Exception) -> bool:
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    name = type(e).__name__.lower()
    msg = str(e).lower()
    return any(t in name or t in msg for t in
               ("ratelimit", "rate limit", "429", "timeout", "timed out", "connection",
                "overloaded", "temporarily unavailable"))


# Shared async Groq client backing the rails LLM, created on first use.
_groq_client: Optional[AsyncGroq] = None


def _get_groq() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set (see backend/env.example)")
        _groq_client = AsyncGroq(api_key=key)
    return _groq_client


_sync_groq_client: Optional[Groq] = None


def _get_sync_groq() -> Groq:
    """Sync client for NeMo's sync code path. Separate from the async client:
    an httpx AsyncClient must not be driven from a second event loop."""
    global _sync_groq_client
    if _sync_groq_client is None:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set (see backend/env.example)")
        _sync_groq_client = Groq(api_key=key)
    return _sync_groq_client


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "guardrails_config")

GUARDRAILS_STATUS: Dict[str, Any] = {
    "enabled": False,
    "model": GUARD_MODEL,
    "fail_mode": FAIL_MODE,
    "error": None,          # init-time error
    "checks_run": 0,
    "blocks": 0,
    "errors": 0,            # runtime rail errors (after retries)
    "retries": 0,           # transient errors that were retried
    "last_error": None,     # most recent rail failure, for observability
}


class GroqLLM(LLM):
    """Minimal LangChain text LLM backed by the Groq API."""

    model: str = GUARD_MODEL

    @property
    def _llm_type(self) -> str:  # noqa: D401
        return "groq"

    def _request(self, prompt: str) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": (
                    "You are a strict content-safety classifier. "
                    "Reply with only one word: 'yes' or 'no'."
                )},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }

    @staticmethod
    def _verdict(resp: Any) -> str:
        out = resp.choices[0].message.content
        if not isinstance(out, str) or not out.strip():
            # An empty verdict must not be parsed as "safe".
            raise RuntimeError("guardrail model returned an empty verdict")
        return out

    @staticmethod
    def _should_retry(attempt: int, e: Exception) -> bool:
        if attempt == _RETRIES - 1 or not _is_transient(e):
            return False
        GUARDRAILS_STATUS["retries"] += 1
        return True

    async def _acall(self, prompt: str, stop: Optional[List[str]] = None,
                     run_manager: Any = None, **kwargs: Any) -> str:
        for attempt in range(_RETRIES):
            try:
                resp = await _get_groq().chat.completions.create(**self._request(prompt))
                return self._verdict(resp)
            except RuntimeError:
                raise
            except Exception as e:
                if not self._should_retry(attempt, e):
                    raise
                await asyncio.sleep(_BASE_DELAY_S * (2 ** attempt))
        raise AssertionError("unreachable")

    def _call(self, prompt: str, stop: Optional[List[str]] = None,
              run_manager: Any = None, **kwargs: Any) -> str:
        """Synchronous path, using the sync Groq client.

        NeMo may call this from inside the server's event loop. The previous
        version ran ``_acall`` via ``asyncio.run`` on a helper thread, which drove
        the shared ``AsyncGroq`` (httpx AsyncClient) from a second event loop —
        unsupported, and prone to "attached to a different loop" errors. A plain
        blocking client is correct here (and NeMo's async path uses ``_acall``).
        """
        for attempt in range(_RETRIES):
            try:
                resp = _get_sync_groq().chat.completions.create(**self._request(prompt))
                return self._verdict(resp)
            except RuntimeError:
                raise
            except Exception as e:
                if not self._should_retry(attempt, e):
                    raise
                time.sleep(_BASE_DELAY_S * (2 ** attempt))
        raise AssertionError("unreachable")


_rails = None


def init_guardrails() -> None:
    """Build the NeMo rails engine once. Called from server startup (not at
    import). Until it succeeds, rails report "not initialised" and fail closed."""
    global _rails
    if _rails is not None:
        return
    try:
        from nemoguardrails import LLMRails, RailsConfig

        cfg = RailsConfig.from_path(CONFIG_PATH)
        _rails = LLMRails(cfg, llm=GroqLLM())
        GUARDRAILS_STATUS["enabled"] = True
        GUARDRAILS_STATUS["error"] = None
        log.info("NeMo Guardrails initialised (model=%s)", GUARD_MODEL)
    except Exception as e:  # pragma: no cover - defensive
        GUARDRAILS_STATUS["error"] = str(e)
        log.error("Guardrails init failed: %s", e)



def _interpret(result: Any) -> Dict[str, Any]:
    """Turn a NeMo GenerationResponse into {blocked, reason, rail}."""
    messages: List[Dict[str, Any]] = []
    log_obj = None
    resp = getattr(result, "response", result)
    log_obj = getattr(result, "log", None)

    if isinstance(resp, dict):
        messages = [resp]
    elif isinstance(resp, list):
        messages = [m for m in resp if isinstance(m, dict)]

    # 1) exception message (enable_rails_exceptions) -> deterministic block
    for m in messages:
        if m.get("role") == "exception":
            content = m.get("content")
            if isinstance(content, dict):
                reason = content.get("message") or content.get("type") or "blocked"
            else:
                reason = str(content)
            return {"blocked": True, "reason": reason, "rail": "self check"}

    # 2) fall back to activated-rails log
    if log_obj is not None:
        for ar in (getattr(log_obj, "activated_rails", None) or []):
            if getattr(ar, "stop", False):
                return {
                    "blocked": True,
                    "reason": f"blocked by '{getattr(ar, 'name', 'rail')}'",
                    "rail": getattr(ar, "name", "self check"),
                }

    return {"blocked": False, "reason": "passed", "rail": None}


def _unavailable(which: str, reason: str, started: float) -> Dict[str, Any]:
    """Verdict when a rail could not run: block unless fail mode is 'open'."""
    block = FAIL_MODE == "closed"
    if block:
        GUARDRAILS_STATUS["blocks"] += 1
    return {"allowed": not block, "blocked": block, "unavailable": True,
            "rail": f"{which} rail", "reason": f"{reason} (fail-{FAIL_MODE})",
            "latency_ms": int((time.time() - started) * 1000)}


async def _generate(messages: List[Dict[str, str]], which: str) -> Dict[str, Any]:
    started = time.time()
    if _rails is None:
        return _unavailable(which, f"guardrails not initialised: {GUARDRAILS_STATUS['error']}",
                            started)
    try:
        from nemoguardrails.rails.llm.options import GenerationOptions

        result = await _rails.generate_async(
            messages=messages,
            options=GenerationOptions(rails=[which], log={"activated_rails": True}),
        )
        verdict = _interpret(result)
        GUARDRAILS_STATUS["checks_run"] += 1
        if verdict["blocked"]:
            GUARDRAILS_STATUS["blocks"] += 1
        return {
            "allowed": not verdict["blocked"],
            "blocked": verdict["blocked"],
            "rail": verdict["rail"],
            "reason": verdict["reason"],
            "latency_ms": int((time.time() - started) * 1000),
        }
    except Exception as e:
        log.error("Guardrails %s check error (fail-%s): %s", which, FAIL_MODE, e)
        GUARDRAILS_STATUS["errors"] = GUARDRAILS_STATUS.get("errors", 0) + 1
        GUARDRAILS_STATUS["last_error"] = f"{which} rail error: {e}"
        return _unavailable(which, f"error: {e}", started)


async def check_input(text: str) -> Dict[str, Any]:
    """Run the input rail on a user message."""
    return await _generate([{"role": "user", "content": text}], "input")


async def check_output(user_text: str, bot_text: str) -> Dict[str, Any]:
    """Run the output rail on the assistant's final answer."""
    return await _generate(
        [{"role": "user", "content": user_text},
         {"role": "assistant", "content": bot_text}],
        "output",
    )
