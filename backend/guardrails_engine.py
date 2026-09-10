"""NeMo Guardrails integration for MESH Support.

Wraps Groq's openai/gpt-oss-120b as a LangChain-compatible model
and feeds it into a NeMo `LLMRails` engine that runs LLM-based self-check rails:
  * input rail  -> blocks jailbreak / prompt-injection / unsafe / off-topic messages
  * output rail -> moderates the assistant's final answer before it reaches the user

Exposes async `check_input` / `check_output` helpers used by server.py. Both are
fail-open (allow on internal error) so the guardrail layer can never take the app
down, while still recording the failure for observability.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from langchain_core.language_models.llms import LLM

from groq import AsyncGroq

log = logging.getLogger("guardrails")

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GUARD_MODEL = os.environ.get("GUARDRAILS_MODEL",
                             os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))

# Shared async Groq client backing the rails LLM.
_groq = AsyncGroq(api_key=GROQ_API_KEY)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "guardrails_config")

GUARDRAILS_STATUS: Dict[str, Any] = {
    "enabled": False,
    "model": GUARD_MODEL,
    "error": None,          # init-time error
    "checks_run": 0,
    "blocks": 0,
    "errors": 0,            # runtime rail errors (fail-open events)
    "last_error": None,     # most recent fail-open reason, for observability
}


class GroqLLM(LLM):
    """Minimal LangChain text LLM backed by the Groq API."""

    api_key: str
    model: str = GUARD_MODEL

    @property
    def _llm_type(self) -> str:  # noqa: D401
        return "groq"

    async def _acall(self, prompt: str, stop: Optional[List[str]] = None,
                     run_manager: Any = None, **kwargs: Any) -> str:
        resp = await _groq.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "You are a strict content-safety classifier. "
                    "Reply with only one word: 'yes' or 'no'."
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        out = resp.choices[0].message.content
        return out if isinstance(out, str) else str(out)

    def _call(self, prompt: str, stop: Optional[List[str]] = None,
              run_manager: Any = None, **kwargs: Any) -> str:
        """Synchronous bridge to ``_acall`` that is safe under a running loop.

        NeMo may invoke this sync method from inside the server's async event
        loop. The old ``get_event_loop().run_until_complete(...)`` raises
        "event loop is already running" there, and that error was then swallowed
        by the fail-open handler, silently disabling the rail (issue L3). We
        instead detect a running loop and, if present, drive the coroutine to
        completion on a dedicated worker thread.
        """
        def _run() -> str:
            return asyncio.run(self._acall(prompt, stop, run_manager, **kwargs))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()  # no loop active in this thread -> safe to run directly

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_run).result()


_rails = None


def _init() -> None:
    global _rails
    try:
        from nemoguardrails import LLMRails, RailsConfig

        cfg = RailsConfig.from_path(CONFIG_PATH)
        _rails = LLMRails(cfg, llm=GroqLLM(api_key=GROQ_API_KEY))
        GUARDRAILS_STATUS["enabled"] = True
        GUARDRAILS_STATUS["error"] = None
        log.info("NeMo Guardrails initialised (model=%s)", GUARD_MODEL)
    except Exception as e:  # pragma: no cover - defensive
        GUARDRAILS_STATUS["error"] = str(e)
        log.error("Guardrails init failed: %s", e)


_init()


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


async def _generate(messages: List[Dict[str, str]], which: str) -> Dict[str, Any]:
    from nemoguardrails.rails.llm.options import GenerationOptions

    started = time.time()
    if _rails is None:
        return {"allowed": True, "blocked": False, "rail": None,
                "reason": "guardrails disabled", "latency_ms": 0}
    try:
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
    except Exception as e:  # fail-open
        log.error("Guardrails %s check error: %s", which, e)
        GUARDRAILS_STATUS["errors"] = GUARDRAILS_STATUS.get("errors", 0) + 1
        GUARDRAILS_STATUS["last_error"] = f"{which} rail error: {e}"
        return {"allowed": True, "blocked": False, "rail": None,
                "reason": f"error (fail-open): {e}",
                "latency_ms": int((time.time() - started) * 1000)}


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
