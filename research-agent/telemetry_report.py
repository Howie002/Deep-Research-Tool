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
        "userId": user_id or None,
        "userEmail": user_email or None,
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
