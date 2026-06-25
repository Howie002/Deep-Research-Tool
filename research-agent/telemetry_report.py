"""
Cross-tool usage telemetry shim — Deep Research variant of the canonical
per-tool shim. Reports one LLM call to the Foundation AI Dashboard's usage
endpoint so Deep Research shows up in the admin Telemetry & Usage tab.

Secret + dashboard URL come from config (loaded from .env by python-dotenv).
Sending is async-aware: inside the CrewAI event loop it fires via
asyncio.create_task (non-blocking); in a sync context it falls back to a short
blocking POST. Best-effort: HMAC-signs the raw body, swallows every error, and
no-ops if the secret is unset, so telemetry never slows or breaks a research run.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx

from config import DASHBOARD_INTERNAL_URL, TELEMETRY_HMAC_SECRET

_TOOL_ID = "deep-research-agent"  # must match the dashboard registry id
_URL = DASHBOARD_INTERNAL_URL.rstrip("/") + "/api/telemetry/usage"

# Hold references to in-flight fire-and-forget tasks so the loop doesn't GC them.
_tasks: set[asyncio.Task] = set()

# Process-global attribution. A research job runs in its own worker subprocess
# (one user per process), so the worker calls set_user() once at startup and
# every report_usage() in that process — the crew's litellm patch AND the
# auxiliary direct calls — attributes to that user without threading it through.
_user_id: str | None = None
_user_email: str | None = None


def set_user(user_id: str | None, user_email: str | None) -> None:
    """Set the user this process's LLM calls are attributed to (called once by
    the research worker after it reads the job)."""
    global _user_id, _user_email
    _user_id = user_id or None
    _user_email = user_email or None


async def _send_async(body: bytes, sig: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                _URL,
                content=body,
                headers={"content-type": "application/json", "x-telemetry-sig": sig},
            )
    except Exception:
        pass  # best-effort — never surface telemetry failures into the user path


def report_usage(
    *,
    user_id: str | None = None,
    user_email: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: int | None = None,
    status: str = "ok",
) -> None:
    if not TELEMETRY_HMAC_SECRET:
        return  # not provisioned → skip silently

    payload = {
        "toolId": _TOOL_ID,
        "userId": (user_id or _user_id) or None,
        "userEmail": (user_email or _user_email) or None,
        "model": model,
        "feature": feature,
        "promptTokens": int(prompt_tokens or 0),
        "completionTokens": int(completion_tokens or 0),
        "durationMs": duration_ms,
        "status": status if status in ("ok", "error", "timeout") else "ok",
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(TELEMETRY_HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        task = loop.create_task(_send_async(body, sig))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    else:
        try:
            httpx.post(
                _URL,
                content=body,
                headers={"content-type": "application/json", "x-telemetry-sig": sig},
                timeout=2.0,
            )
        except Exception:
            pass


def report_from_response(resp_json: dict, model: str | None, feature: str,
                         status: str = "ok", duration_ms: int | None = None) -> None:
    """Convenience for the direct chat/completions calls: pull `usage` off a
    parsed OpenAI-style response and report it (attributed to the process-global
    user set by the worker). Best-effort; never raises."""
    try:
        usage = (resp_json or {}).get("usage") or {}
        report_usage(
            model=model,
            feature=feature,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            duration_ms=duration_ms,
            status=status,
        )
    except Exception:
        pass
