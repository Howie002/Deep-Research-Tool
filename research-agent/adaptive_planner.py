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
    "You decompose a research query into 3-8 GENERIC verifiable claims that can be "
    "resolved by fetching primary sources. Do NOT guess the subject's field, "
    "specialty, or domain — that's what the investigation is for. "
    "Write each claim as an open-ended attribute to investigate, phrased neutrally.\n\n"
    "For PERSON queries, standard decomposition:\n"
    "  - The subject's current employer / primary affiliation\n"
    "  - The subject's past employment / notable prior roles\n"
    "  - The subject's education (institutions + degrees + years)\n"
    "  - The subject's professional credentials / licenses / certifications\n"
    "  - The subject's publications / public works / notable projects\n"
    "  - Specific attributes named in the query itself (e.g. 'former professor at X' → "
    "    'The subject held a faculty position at X')\n\n"
    "For ORGANISATION or TOPIC queries, decompose similarly into structural / "
    "attribute-level claims, not into opinions or interpretations.\n\n"
    "DO NOT invent claims that assume a specific domain based on the subject's name, "
    "apparent background, or the query phrasing. If the query says 'former professor' "
    "it means they once taught — it does NOT specify the subject matter they taught. "
    "The department, the field, and the research topic must emerge from EVIDENCE, not "
    "be invented in the decomposition.\n\n"
    "Output STRICT JSON and nothing else: "
    '{"claims": [{"text": "<neutral, verifiable statement>", "priority": 0.0-1.0}, ...]}'
)


def _decompose_user_prompt(query: str) -> str:
    return (
        f"QUERY: {query}\n\n"
        "Produce 3-8 generic, verifiable claims. Set priority 1.0 for claims central "
        "to the query's main subject, 0.6 for important supporting claims, 0.3 for "
        "peripheral context. Each claim should be answerable by fetching one or two "
        "authoritative sources.\n\n"
        "Remember: claims should describe WHAT to find (an attribute), not WHAT IT WILL "
        "BE (a specific value). 'The subject's current employer' is correct. "
        "'The subject is employed at Harvard' is NOT correct — that prejudges the answer.\n\n"
        "Output only the JSON object."
    )


_NEXT_ACTION_SYSTEM = (
    "You are a research planner. You see the live state of an ongoing investigation "
    "(current claims, their support, recent actions, URLs surfaced but not yet fetched, "
    "and remaining budget) and choose the single most valuable next action.\n\n"
    "Your output is one of these JSON shapes (and NOTHING else):\n"
    '  {"type": "search", "query": "<search query>", "target_claim_id": "<id or null>"}\n'
    '  {"type": "fetch",  "url": "<url>", "target_claim_id": "<id or null>"}\n'
    '  {"type": "stop",   "reason": "<short reason>"}\n\n'
    "Rules:\n"
    " - **If there are URLs in 'AVAILABLE TO FETCH' that look promising for an open "
    "claim, prefer 'fetch' over another 'search'.** Searches produce URLs; fetches "
    "produce evidence. You must fetch to make progress.\n"
    " - Pick actions that close the highest-uncertainty claim first.\n"
    " - Prefer primary sources (official org pages, government registries, the subject's "
    "own site / LinkedIn) over aggregator pages (scispace, zoominfo) when both are "
    "available.\n"
    " - If a claim involves a specific credential (JD / PhD / medical license / bar admission), "
    "the right action is usually to search the relevant registry, then fetch the registry "
    "page when it appears.\n"
    " - Do NOT re-search for something already attempted — look at 'recent actions'. If a "
    "search has returned results but you haven't fetched anything, fetch one of those URLs "
    "before searching again.\n"
    " - Return 'stop' when claims are satisfactorily answered, or when you've tried "
    "several paths and further effort is unlikely to help within budget."
)


def _next_action_user_prompt(cm: ClaimsModel, available_urls: Optional[list[dict]] = None) -> str:
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

    # URLs surfaced by searches but not yet fetched — the planner needs
    # to see these to propose "fetch" actions at all.
    url_lines: list[str] = []
    for item in (available_urls or [])[:10]:
        u = item.get("url", "")
        if not u:
            continue
        title = item.get("title", "")
        snippet = item.get("snippet", "").replace("\n", " ")[:140]
        parts = [f"  {u}"]
        if title:
            parts.append(f"    title: {title[:120]}")
        if snippet:
            parts.append(f"    snippet: {snippet}")
        url_lines.append("\n".join(parts))

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
        "AVAILABLE TO FETCH (URLs surfaced by searches, not yet fetched):\n"
        + ("\n".join(url_lines) if url_lines else "(none — run a search first)")
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


def next_action(
    cm: ClaimsModel,
    llm: LLMFn,
    available_urls: Optional[list[dict]] = None,
) -> dict:
    """Return a dict describing the next action.

    Always returns a valid action dict — on LLM/parse failure falls back
    to either fetching an available URL (if the planner sees one and the
    claims are open), searching the highest-priority open claim's text,
    or stopping.

    available_urls: optional [{"url", "title", "snippet"}, ...] — URLs
    surfaced by recent searches that have not yet been fetched. Makes it
    possible for the planner to propose `fetch` actions at all.
    """
    raw = llm(_NEXT_ACTION_SYSTEM, _next_action_user_prompt(cm, available_urls))
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
    open_claim = cm.highest_priority_open()
    # Prefer fetching an available URL over yet another search — that's
    # how the loop actually makes progress.
    if available_urls and open_claim:
        url = (available_urls[0] or {}).get("url")
        if url:
            return {
                "type": "fetch",
                "url": url,
                "target_claim_id": open_claim.id,
            }
    if open_claim:
        return {
            "type": "search",
            "query": open_claim.text[:200],
            "target_claim_id": open_claim.id,
        }
    return {"type": "stop", "reason": "no open claims and planner unavailable"}
