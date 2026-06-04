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

from claims import Claim, ClaimsModel, ClaimStatus

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


def _decompose_user_prompt(query: str, clarifications: str = "") -> str:
    clarif_block = ""
    if clarifications and clarifications.strip():
        clarif_block = (
            "\n\nUSER CLARIFICATIONS — these refine scope and priority. Apply them when "
            "deciding which claims to include and how to set priorities:\n"
            + clarifications.strip()
            + "\n"
        )
    return (
        f"QUERY: {query}{clarif_block}\n\n"
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
    " - **ENTITY ANCHORING (critical):** the QUERY names a specific subject. Every search "
    "query you produce MUST include the subject's full distinguishing identity — their full "
    "name PLUS a disambiguator drawn from the QUERY (their organization, role, location, or "
    "field). Never search a bare common name alone. If a surfaced URL, title, or snippet is "
    "clearly about a DIFFERENT entity that merely shares a name (a different person, a "
    "namesake company, an athlete vs. a businessman, etc.), do NOT fetch it and do NOT treat "
    "it as evidence — pick a different action.\n"
    " - **FETCH-FIRST (critical for progress):** searches only produce URLs — only "
    "FETCHES produce the evidence that resolves claims. If 'AVAILABLE TO FETCH' has ANY "
    "on-entity URL that could touch an open claim, you MUST 'fetch' it rather than run "
    "another 'search'. Do NOT keep searching for better phrasings while on-entity URLs sit "
    "unfetched — fetch them first, then decide. Only search again when the queue holds "
    "nothing on-entity and unfetched.\n"
    " - Pick actions that close the highest-uncertainty claim first.\n"
    " - **Corroboration rule:** If your target claim is already PARTIAL (status=partial) "
    "with only ONE supporting URL, your next action MUST seek corroboration from a "
    "DIFFERENT source category — do NOT repeat the search query that found the first "
    "source, and do NOT fetch another aggregator page (rocketreach / zoominfo / signalhire / "
    "scispace) if the existing source is already one. The point is a SECOND independent "
    "source for the same fact, not another mention of it. Try a primary org page, a "
    "registry, the subject's own site, or a news/press release angle.\n"
    " - Prefer primary sources (official org pages, government registries, the subject's "
    "own site / LinkedIn) over aggregator pages when both are available.\n"
    " - If a claim involves a specific credential (JD / PhD / medical license / bar admission), "
    "the right action is usually to search the relevant registry, then fetch the registry "
    "page when it appears.\n"
    " - Do NOT re-search for something already attempted — look at 'recent actions'. If a "
    "search has returned results but you haven't fetched anything, fetch one of those URLs "
    "before searching again.\n"
    " - Return 'stop' ONLY if claims are satisfactorily answered AND the budget is barely "
    "started. Do not stop just because the last few turns produced no updates — the loop "
    "has a separate strategist mechanism for that."
)


def _next_action_user_prompt(
    cm: ClaimsModel,
    available_urls: Optional[list[dict]] = None,
    clarifications: str = "",
) -> str:
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
    clarif_block = ""
    if clarifications and clarifications.strip():
        clarif_block = (
            "USER CLARIFICATIONS (constrain action selection — stay aligned to these):\n"
            + clarifications.strip()
            + "\n\n"
        )
    return (
        f"QUERY: {cm.query}\n\n"
        + clarif_block
        + f"BUDGET: fetches {budget.fetches_used}/{budget.max_fetches}, "
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


def decompose_query(query: str, llm: LLMFn, clarifications: str = "") -> list[dict]:
    """Return a list of {text, priority} dicts to be added to the claims model.

    On any LLM/parse failure returns an empty list — the caller can fall
    back to a single generic claim like 'answer the user's query'.
    """
    raw = llm(_DECOMPOSE_SYSTEM, _decompose_user_prompt(query, clarifications))
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
    clarifications: str = "",
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
    raw = llm(_NEXT_ACTION_SYSTEM, _next_action_user_prompt(cm, available_urls, clarifications))
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


# ── Strategist turn (meta-loop reflection) ───────────────────────────────────
# Triggered (a) when the tactical loop has stalled — consecutive turns with
# no claim updates — and (b) periodically every N tactical loops, even when
# the loop is making progress, so re-planning isn't only reactive.
#
# Output: a single LLM call that produces a diagnosis + plan revisions
# (priority changes, abandonments, new claims) AND a recommended next
# action that explicitly breaks whatever stalemate it diagnosed.


_STRATEGIST_SYSTEM = (
    "You are a research strategist. The tactical research loop has either "
    "stalled or hit a periodic re-plan checkpoint, and you're being asked to "
    "step back and revise the plan rather than pick another tactical action.\n\n"
    "Look at the live claims model and recent actions, then:\n"
    "  1. **Diagnose** why progress has stopped. Common patterns: all evidence "
    "from one aggregator (rocketreach / zoominfo / signalhire); claim too vague "
    "to verify directly; subject's online profile is genuinely sparse; we keep "
    "fetching gated pages (LinkedIn 403, paywalled sources); the same query is "
    "being repeated; we have a partial claim but every fetch returns the same "
    "fact already in the snippet.\n"
    "  2. **Decide what to change in the plan:**\n"
    "     - Update priorities — raise claims most likely to yield given remaining "
    "budget; lower or abandon dead-ends.\n"
    "     - Abandon claims that are hopeless within remaining budget. Be direct: "
    "abandoning is a valid move, not a failure.\n"
    "     - Add NEW claims that the current set is missing — narrower formulations "
    "of stuck claims, or alternative angles (e.g. 'subject lists a personal "
    "website', 'subject is named in a press release in 2023').\n"
    "  3. **Recommend the next concrete action** that breaks the stalemate. Do NOT "
    "repeat a recent action verbatim. If the stuck claim is at PARTIAL with one "
    "aggregator source, propose a non-aggregator probe. If the loop has been "
    "searching variations of the same query, propose a structurally different "
    "search (e.g. site:registry.org instead of name+org keywords).\n\n"
    "Output STRICT JSON and nothing else:\n"
    "{\n"
    '  "diagnosis": "<one or two sentences>",\n'
    '  "priority_updates": [{"claim_id": "<id>", "new_priority": 0.0-1.0}],\n'
    '  "abandon": ["<claim_id>", ...],\n'
    '  "new_claims": [{"text": "<concrete verifiable claim>", "priority": 0.0-1.0}],\n'
    '  "next_action": {"type": "search"|"fetch", "query": "<...>", "url": "<...>", "target_claim_id": "<id or null>"}\n'
    "}\n\n"
    "Empty arrays are fine if there's nothing to revise. The next_action is "
    "REQUIRED — even if the plan revision is the main contribution, give the "
    "tactical loop something concrete to execute next."
)


def _strategist_user_prompt(cm: ClaimsModel, available_urls: Optional[list[dict]] = None) -> str:
    # Render claims grouped by status so the strategist sees the shape of progress.
    by_status: dict[str, list[Claim]] = {}
    for c in cm.claims.values():
        by_status.setdefault(c.status.value, []).append(c)

    sections: list[str] = []
    for status_label in ("supported", "partial", "investigating", "unknown", "refuted", "abandoned"):
        cs = by_status.get(status_label, [])
        if not cs:
            continue
        cs.sort(key=lambda c: -c.priority)
        lines = [f"  ({status_label.upper()}, {len(cs)})"]
        for c in cs:
            sup = ", ".join(s.url[:60] for s in c.support[:3]) or "-"
            lines.append(
                f"  - id={c.id} priority={c.priority:.2f} conf={c.confidence:.2f} "
                f"attempts={c.attempts}\n"
                f"    claim: {c.text}\n"
                f"    support: {sup}"
            )
        sections.append("\n".join(lines))

    actions_lines: list[str] = []
    for a in cm.recent_actions(8):
        act = a.get("action", {})
        descr = act.get("query") or act.get("url") or act.get("reason") or ""
        res = (a.get("result") or "").replace("\n", " ")[:120]
        actions_lines.append(f"  - [{act.get('type','?')}] {descr[:80]}  →  {res or '(no result)'}")

    url_lines: list[str] = []
    for item in (available_urls or [])[:8]:
        u = item.get("url", "")
        if not u:
            continue
        url_lines.append(f"  - {u}  ({item.get('title','')[:60]})")

    budget = cm.budget
    return (
        f"QUERY: {cm.query}\n\n"
        f"BUDGET: fetches {budget.fetches_used}/{budget.max_fetches}, "
        f"searches {budget.searches_used}/{budget.max_searches}, "
        f"llm_calls {budget.llm_calls_used}/{budget.max_llm_calls}, "
        f"loops {budget.loops_used}/{budget.max_loop_iterations}, "
        f"wallclock {budget.remaining_wallclock():.0f}s left\n\n"
        "CLAIMS BY STATUS:\n"
        + ("\n\n".join(sections) if sections else "  (none)")
        + "\n\n"
        "RECENT ACTIONS (last 8):\n"
        + ("\n".join(actions_lines) if actions_lines else "  (none)")
        + "\n\n"
        "AVAILABLE TO FETCH (URLs surfaced, not yet fetched):\n"
        + ("\n".join(url_lines) if url_lines else "  (none)")
        + "\n\n"
        "Diagnose the stall, revise the plan, and recommend the next action. "
        "Output only the JSON."
    )


def strategic_replan(
    cm: ClaimsModel,
    llm: LLMFn,
    available_urls: Optional[list[dict]] = None,
    clarifications: str = "",
) -> dict:
    """One LLM call returning a diagnosis + plan revisions + next action.

    Always returns a dict with the expected shape — on parse failure the
    caller still gets a usable fallback `next_action` (typically a search
    on the highest-priority open claim's text) plus an explanatory diagnosis.
    """
    user = _strategist_user_prompt(cm, available_urls)
    if clarifications and clarifications.strip():
        user = (
            f"USER CLARIFICATIONS (apply throughout your re-plan):\n"
            f"{clarifications.strip()}\n\n"
            + user
        )
    raw = llm(_STRATEGIST_SYSTEM, user)
    parsed = _parse_json(raw or "")

    out: dict = {
        "diagnosis":        "",
        "priority_updates": [],
        "abandon":          [],
        "new_claims":       [],
        "next_action":      None,
        "parse_failed":     False,
    }

    if not isinstance(parsed, dict):
        out["parse_failed"] = True
        out["diagnosis"] = "Strategist LLM call failed or returned unparseable output."
        # Pick a tactical fallback so the loop can still progress.
        oc = cm.highest_priority_open()
        if available_urls:
            url = (available_urls[0] or {}).get("url")
            if url:
                out["next_action"] = {
                    "type": "fetch", "url": url,
                    "target_claim_id": oc.id if oc else None,
                }
        elif oc:
            out["next_action"] = {
                "type": "search", "query": oc.text[:200],
                "target_claim_id": oc.id,
            }
        else:
            out["next_action"] = {"type": "stop", "reason": "strategist unavailable and no open claims"}
        return out

    out["diagnosis"] = str(parsed.get("diagnosis", "")).strip()[:500]

    # Priority updates: only apply to claims that actually exist.
    for upd in parsed.get("priority_updates", []) or []:
        if not isinstance(upd, dict):
            continue
        cid = str(upd.get("claim_id", "")).strip()
        if cid not in cm.claims:
            continue
        try:
            new_p = float(upd.get("new_priority", -1))
        except (TypeError, ValueError):
            continue
        if 0.0 <= new_p <= 1.0:
            out["priority_updates"].append({"claim_id": cid, "new_priority": new_p})

    # Abandon list: only valid claim ids that aren't already terminal.
    for cid in parsed.get("abandon", []) or []:
        cid = str(cid).strip()
        if cid in cm.claims and not cm.claims[cid].status.terminal:
            out["abandon"].append(cid)

    # New claims: dedupe against existing claim text.
    seen_texts = {re.sub(r"\s+", " ", c.text.lower()) for c in cm.claims.values()}
    for nc in parsed.get("new_claims", []) or []:
        if not isinstance(nc, dict):
            continue
        text = str(nc.get("text", "")).strip()
        if not text or len(text) < 8:
            continue
        norm = re.sub(r"\s+", " ", text.lower())
        if norm in seen_texts:
            continue
        seen_texts.add(norm)
        try:
            priority = float(nc.get("priority", 0.5))
        except (TypeError, ValueError):
            priority = 0.5
        out["new_claims"].append({
            "text": text,
            "priority": max(0.0, min(1.0, priority)),
        })

    # Next action: re-use the same parsing logic as next_action() to keep
    # behaviour consistent. Default to a tactical fallback if missing.
    na = parsed.get("next_action") or {}
    if isinstance(na, dict):
        t = str(na.get("type", "")).strip().lower()
        if t == "search" and str(na.get("query", "")).strip():
            out["next_action"] = {
                "type": "search",
                "query": str(na["query"]).strip(),
                "target_claim_id": na.get("target_claim_id") or None,
            }
        elif t == "fetch" and str(na.get("url", "")).startswith(("http://", "https://")):
            out["next_action"] = {
                "type": "fetch",
                "url": str(na["url"]).strip(),
                "target_claim_id": na.get("target_claim_id") or None,
            }
    if not out["next_action"]:
        oc = cm.highest_priority_open()
        if oc:
            out["next_action"] = {
                "type": "search",
                "query": oc.text[:200],
                "target_claim_id": oc.id,
            }
        else:
            out["next_action"] = {"type": "stop", "reason": "strategist returned no actionable next step"}

    return out
