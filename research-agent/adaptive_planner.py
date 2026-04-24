"""
adaptive_planner.py — The planner for the adaptive research loop.

Two responsibilities:
  1. decompose_query(query)  — at start of a run, split a natural-language
     query into 3-8 concrete verifiable claims (the "completeness model").
  2. next_action(claims)     — at each loop iteration, look at current state
     + budget and pick the single most valuable next action (search, fetch,
     raise a new claim, or stop).

Both are single-LLM-call primitives and MUST tolerate LLM flakiness —
parse errors return a sensible default (an empty plan or a "search the
original query" fallback) rather than raising.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from claims import Claim, ClaimsModel

LLMFn = Callable[[str, str], Optional[str]]
#   (system: str, user: str) -> content: str | None


# ── Prompt fragments ──────────────────────────────────────────────────────────


_DECOMPOSE_SYSTEM = (
    "You decompose a research query into 3-8 concrete verifiable claims. "
    "Each claim is a short statement that would appear in a completed report as a fact, "
    "written so it can be independently verified by reading a web page. "
    "Avoid meta-claims ('this person is interesting'); prefer specific attributes "
    "(employer, role, education, credentials, publications, affiliations). "
    "Output STRICT JSON and nothing else: "
    '{"claims": [{"text": "...", "priority": 0.0-1.0}, ...]}'
)


def _decompose_user_prompt(query: str) -> str:
    return (
        f"QUERY: {query}\n\n"
        "Produce 3-8 concrete, verifiable claims. Set priority 1.0 for claims central "
        "to the query's main subject, 0.6 for important supporting claims, 0.3 for "
        "peripheral context. Output only the JSON object."
    )


_NEXT_ACTION_SYSTEM = (
    "You are a research planner. You see the live state of an ongoing investigation "
    "(current claims, their support, recent actions, remaining budget) and choose the "
    "single most valuable next action.\n\n"
    "Your output is one of these JSON shapes (and NOTHING else):\n"
    '  {"type": "search", "query": "<search query>", "target_claim_id": "<id or null>"}\n'
    '  {"type": "fetch",  "url": "<url>", "target_claim_id": "<id or null>"}\n'
    '  {"type": "stop",   "reason": "<short reason>"}\n\n'
    "Rules:\n"
    " - Pick actions that close the highest-uncertainty claim first.\n"
    " - Prefer a targeted search query that will surface a PRIMARY source (official "
    "organisational pages, government registries, the subject's own site) over a "
    "generic web search.\n"
    " - If a claim involves a specific credential (JD / PhD / medical license), "
    "the right action is usually to search the relevant registry.\n"
    " - Do NOT re-search for something already attempted — look at 'recent actions'.\n"
    " - Return 'stop' when claims are satisfactorily answered, or when you've tried "
    "several paths and further effort is unlikely to help within budget."
)


def _next_action_user_prompt(cm: ClaimsModel) -> str:
    # Render the current state compactly. Top 10 claims max; recent 6 actions.
    claim_lines: list[str] = []
    for c in sorted(cm.claims.values(), key=lambda c: (-c.priority, c.attempts))[:10]:
        sup_src = ", ".join(
            (s.url[:70] + "…" if len(s.url) > 70 else s.url) for s in c.support[:3]
        )
        claim_lines.append(
            f"- id={c.id} status={c.status.value} conf={c.confidence:.2f} "
            f"priority={c.priority:.2f} attempts={c.attempts}\n"
            f"    claim: {c.text}\n"
            f"    support: {sup_src or '(none)'}"
        )

    action_lines: list[str] = []
    for a in cm.recent_actions(6):
        act = a.get("action", {})
        t = act.get("type", "?")
        descr = act.get("query") or act.get("url") or act.get("reason") or ""
        res = (a.get("result") or "").replace("\n", " ")[:120]
        action_lines.append(f"- [{t}] {descr[:80]}  →  {res or '(no result)'}")

    budget = cm.budget
    return (
        f"QUERY: {cm.query}\n\n"
        f"BUDGET: fetches {budget.fetches_used}/{budget.max_fetches}, "
        f"searches {budget.searches_used}/{budget.max_searches}, "
        f"llm_calls {budget.llm_calls_used}/{budget.max_llm_calls}, "
        f"wallclock {cm.budget.remaining_wallclock():.0f}s left, "
        f"loops {budget.loops_used}/{budget.max_loop_iterations}\n\n"
        "CLAIMS (top 10 by priority):\n"
        + ("\n".join(claim_lines) if claim_lines else "(none yet)")
        + "\n\n"
        "RECENT ACTIONS (last 6):\n"
        + ("\n".join(action_lines) if action_lines else "(none)")
        + "\n\n"
        "Pick the single next action. Output only the JSON."
    )


# ── Parsing helpers ──────────────────────────────────────────────────────────


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── Public API ───────────────────────────────────────────────────────────────


def decompose_query(query: str, llm: LLMFn) -> list[dict]:
    """Return a list of {text, priority} dicts to be added to the claims model.

    On any LLM/parse failure returns an empty list — the caller can fall
    back to a single generic claim like 'answer the user's query'.
    """
    raw = llm(_DECOMPOSE_SYSTEM, _decompose_user_prompt(query))
    parsed = _parse_json(raw or "")
    if not isinstance(parsed, dict):
        return []
    out: list[dict] = []
    for c in parsed.get("claims", []) or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text", "")).strip()
        if not text:
            continue
        try:
            priority = float(c.get("priority", 0.5))
        except (TypeError, ValueError):
            priority = 0.5
        out.append({"text": text, "priority": max(0.0, min(1.0, priority))})
    return out[:8]


def next_action(cm: ClaimsModel, llm: LLMFn) -> dict:
    """Return a dict describing the next action.

    Always returns a valid action dict — on LLM/parse failure falls back
    to either searching the highest-priority open claim's text or stopping.
    """
    raw = llm(_NEXT_ACTION_SYSTEM, _next_action_user_prompt(cm))
    parsed = _parse_json(raw or "")

    if isinstance(parsed, dict):
        t = str(parsed.get("type", "")).strip().lower()
        if t == "search":
            q = str(parsed.get("query", "")).strip()
            if q:
                return {
                    "type": "search",
                    "query": q,
                    "target_claim_id": parsed.get("target_claim_id") or None,
                }
        elif t == "fetch":
            url = str(parsed.get("url", "")).strip()
            if url.startswith(("http://", "https://")):
                return {
                    "type": "fetch",
                    "url": url,
                    "target_claim_id": parsed.get("target_claim_id") or None,
                }
        elif t == "stop":
            return {
                "type": "stop",
                "reason": str(parsed.get("reason", "planner stopped"))[:200],
            }

    # Fallbacks:
    # If there's an open claim, search for its text.
    open_claim = cm.highest_priority_open()
    if open_claim:
        return {
            "type": "search",
            "query": open_claim.text[:200],
            "target_claim_id": open_claim.id,
        }
    return {"type": "stop", "reason": "no open claims and planner unavailable"}
