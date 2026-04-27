"""
adaptive_worker.py — The budget-driven research loop.

Replaces the linear 4-stage pipeline (Researcher → Analyst → Gap Analyst →
Synthesizer) with a single loop that:
  1. Decomposes the query into concrete verifiable claims.
  2. On each iteration, the planner picks the highest-value next action
     (search or fetch), which is dispatched to the existing tools.
  3. The evaluator reads the result and updates claim statuses + may raise
     new claims.
  4. The loop stops when enough claims are supported OR budget is exhausted
     OR progress stalls.
  5. A deterministic synthesizer renders the final report — every citation
     in the report is a URL that was actually fetched, and every factual
     claim carries its own quote from the source.

Designed to coexist with the linear pipeline rather than replace it — call
it via `run_adaptive(query, depth=...)` or the run.py `adaptive` subcommand.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from claims import Claim, ClaimsModel, ClaimStatus, Evidence, preset_budget
from config import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
from adaptive_planner import decompose_query, next_action, strategic_replan
from adaptive_evaluator import integrate_result


# ── URL harvesting from search output ────────────────────────────────────────
# WebSearchTool returns markdown-formatted search results; parse them back
# into (url, title, snippet) tuples so the planner can see what's available
# to fetch next.

_SEARCH_BLOCK_RE = re.compile(
    r"^\s*\d+\.\s+\*\*(?P<title>[^*]+)\*\*.*?$"             # numbered title line
    r"\n\s*URL:\s*(?P<url>\S+)\s*$"                          # URL: <url>
    r"\n\s*(?P<snippet>[^\n]+)\s*$",                         # snippet line
    re.MULTILINE,
)


def _harvest_search_urls(search_text: str) -> list[dict]:
    """Parse WebSearchTool output into [{url, title, snippet}]."""
    out: list[dict] = []
    for m in _SEARCH_BLOCK_RE.finditer(search_text or ""):
        url = m.group("url").strip()
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "url":     url,
            "title":   m.group("title").strip(),
            "snippet": m.group("snippet").strip(),
        })
    return out


# ── Minimal LLM caller (no LiteLLM / CrewAI dependency) ──────────────────────


def _build_llm_caller(track_fn: Callable[[], None]) -> Callable[[str, str], Optional[str]]:
    """Return an (system, user) -> content callable that tracks usage.

    track_fn is called every time a request is ISSUED (success or failure),
    so budget accounting works even if LM Studio errors.
    """
    def call(system: str, user: str) -> Optional[str]:
        track_fn()
        payload = {
            "model": LM_STUDIO_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
            "presence_penalty": 0.1,
            "frequency_penalty": 0.3,
        }
        # Some LM Studio models support these via extra_body; harmless otherwise.
        payload["repetition_penalty"] = 1.15
        req = urllib.request.Request(
            LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            # Prefer content; fall back to reasoning_content if the model is a thinking variant.
            msg = body.get("choices", [{}])[0].get("message", {}) or {}
            return (msg.get("content") or msg.get("reasoning_content") or "").strip() or None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
            return None
    return call


# ── Sidebar repopulation helpers ─────────────────────────────────────────────
# The existing UI panels (Plan, Notes, Thoughts) listen for `plan_update`,
# `note_add`, and `thought_node` stream events that the linear pipeline emitted
# via tools. The adaptive loop doesn't use those tools — but we can synthesise
# the same events from the claims model so the panels populate live without UI
# code changes.


def _render_plan_markdown(cm: ClaimsModel) -> str:
    """Render the live claims model as a markdown checklist for the Plan tab."""
    if not cm.claims:
        return "## Research Plan\n\n_(decomposing query…)_"
    lines = ["## Research Plan", ""]
    # Group by status so the user sees what's done vs what's open at a glance.
    order = [
        ("supported",     "✓ Supported", True),
        ("partial",       "◐ Partial",   False),
        ("investigating", "↻ Investigating", False),
        ("unknown",       "□ Open",      False),
        ("refuted",       "✗ Refuted",   True),
        ("abandoned",     "— Abandoned", True),
    ]
    by_status: dict[str, list] = {}
    for c in cm.claims.values():
        by_status.setdefault(c.status.value, []).append(c)
    for status_key, label, _checked in order:
        cs = by_status.get(status_key, [])
        if not cs:
            continue
        cs.sort(key=lambda c: -c.priority)
        lines.append(f"**{label}**")
        for c in cs:
            box = "[x]" if c.status.terminal and c.status.value == "supported" else "[ ]"
            attempts = f"  ·  {c.attempts}× tried" if c.attempts else ""
            conf = f"  ·  conf {c.confidence:.2f}" if c.confidence else ""
            lines.append(f"- {box} {c.text}{conf}{attempts}")
        lines.append("")
    b = cm.budget
    lines.append(f"_Budget: fetches {b.fetches_used}/{b.max_fetches} · "
                 f"searches {b.searches_used}/{b.max_searches} · "
                 f"loops {b.loops_used}/{b.max_loop_iterations}_")
    return "\n".join(lines)


def _emit_plan_panel(cm: ClaimsModel, emit) -> None:
    """Push a `plan_update` stream event so the existing Plan tab renders the claims checklist."""
    try:
        emit({"type": "plan_update", "content": _render_plan_markdown(cm)})
    except Exception:
        pass


def _emit_notes_panel(cm: ClaimsModel, emit, claim_ids: list[str]) -> None:
    """For each updated claim with new Evidence, emit a `note_add` event with the latest quote + URL."""
    for cid in claim_ids:
        c = cm.claims.get(cid)
        if not c or not c.support:
            continue
        ev = c.support[-1]   # latest evidence
        note_md = (
            f"**{c.text}**  ·  _{c.status.value}_  ·  conf {c.confidence:.2f}\n\n"
            f"> \"{ev.quote}\"\n\n"
            f"— [{ev.url}]({ev.url})"
        )
        try:
            emit({
                "type":       "note_add",
                "content":    note_md,
                "source_url": ev.url,
                "quote":      ev.quote,
            })
        except Exception:
            pass


def _emit_thought(emit, label: str, rationale: str = "") -> None:
    """Emit a `thought_node` event — surfaces in the Thoughts tab as a reasoning step."""
    import uuid as _uuid
    try:
        emit({
            "type":      "thought_node",
            "id":        _uuid.uuid4().hex[:8],
            "label":     label[:140],
            "rationale": rationale[:400],
        })
    except Exception:
        pass


# ── Prose synthesizer (LLM polish pass) ─────────────────────────────────────


_PROSE_SYSTEM = (
    "You are a research writer. You receive a structured claims model from a "
    "completed investigation: every supported claim comes with one or more "
    "verbatim quotes from real fetched pages, plus the source URL. Your job is "
    "to write a flowing research brief that incorporates these established "
    "facts into a coherent narrative.\n\n"
    "STRICT RULES — these are mechanically enforced after you write:\n"
    "  1. Every URL you cite MUST appear in the supplied evidence list. Do NOT "
    "     invent URLs, recall URLs from training, or paraphrase a URL. URLs not "
    "     in the evidence will be stripped and flagged.\n"
    "  2. Every factual claim must trace to a quoted source. If a claim is "
    "     'partial' (one source) or 'tension' (sources disagree), hedge "
    "     appropriately — e.g. 'according to a single aggregator listing…' or "
    "     'two sources differ on this detail'.\n"
    "  3. Do not assert subjective confidence labels (HIGH / VERIFIED). The "
    "     calling code attaches a mechanical confidence audit; your assertions "
    "     would be ignored.\n"
    "  4. Open / abandoned claims should be acknowledged briefly at the end as "
    "     known unknowns — not papered over with prose.\n\n"
    "Format (Markdown):\n"
    "  ## Summary\n"
    "  Two- to four-sentence executive answer to the original query, drawing on "
    "the supported claims.\n\n"
    "  ## Background\n"
    "  Two or three flowing paragraphs that introduce the subject and what we "
    "know about them. Cite URLs inline as `(URL)` after each factual sentence.\n\n"
    "  ## Findings\n"
    "  Connected paragraphs (not bullet lists) covering the supported claims, "
    "organised by topic — employment, education, credentials, history, etc.\n\n"
    "  ## What Remains Open\n"
    "  Short paragraph on what couldn't be resolved within the budget.\n\n"
    "Write for an intelligent reader who wants a brief, not for the agent "
    "narrating its own activity. No 'the system found' / 'the loop discovered' "
    "framing — describe the SUBJECT, citing sources."
)


def _evidence_for_prose(cm: ClaimsModel) -> str:
    """Compact, prompt-ready dump of the claims model + every quote + URL."""
    parts: list[str] = []
    by_status: dict[str, list[Claim]] = {}
    for c in cm.claims.values():
        by_status.setdefault(c.status.value, []).append(c)
    for status_label in ("supported", "partial", "refuted", "abandoned",
                          "investigating", "unknown"):
        cs = by_status.get(status_label, [])
        if not cs:
            continue
        cs.sort(key=lambda c: -c.confidence)
        parts.append(f"=== {status_label.upper()} ===")
        for c in cs:
            parts.append(f"CLAIM: {c.text}  (confidence {c.confidence:.2f}, "
                         f"{len(c.support)} supporting / {len(c.contradictions)} contradicting)")
            for ev in c.support[:5]:
                parts.append(f"  + \"{ev.quote}\"")
                parts.append(f"    URL: {ev.url}")
            for ev in c.contradictions[:3]:
                parts.append(f"  - tension/contradicts: \"{ev.quote}\"")
                parts.append(f"    URL: {ev.url}")
        parts.append("")
    return "\n".join(parts) if parts else "(no claims)"


def _strip_ghost_urls(prose: str, allowed_urls: set[str]) -> tuple[str, list[str]]:
    """Remove or flag URLs that aren't in `allowed_urls` (post-pass guard).

    Returns (cleaned_prose, list_of_stripped_urls). Mirrors the ghost-citation
    guard from the grounding pass — the prose synthesizer is hard-rule-bound
    to use only URLs from the claims model's evidence.
    """
    url_re = re.compile(r"https?://[^\s)\]\"'>]+")
    stripped: list[str] = []

    def _replace(m):
        url = m.group(0).rstrip(".,;:)]>'\"")
        if url in allowed_urls:
            return m.group(0)
        stripped.append(url)
        return f"[unverified url stripped]"

    cleaned = url_re.sub(_replace, prose)
    return cleaned, list(set(stripped))


def synthesize_prose(cm: ClaimsModel, llm) -> tuple[str, list[str]]:
    """LLM-polish pass: write a narrative report grounded in the claims model.

    Returns (prose_markdown, stripped_ghost_urls). Falls back to the
    deterministic synthesis on any LLM failure.
    """
    # Collect every URL the synthesizer is allowed to cite.
    allowed_urls: set[str] = set()
    for c in cm.claims.values():
        for ev in c.support:
            if ev.url:
                allowed_urls.add(ev.url)
        for ev in c.contradictions:
            if ev.url:
                allowed_urls.add(ev.url)

    user_prompt = (
        f"ORIGINAL QUERY: {cm.query}\n\n"
        f"EVIDENCE FROM THE INVESTIGATION:\n{_evidence_for_prose(cm)}\n\n"
        f"ALLOWED URLS (cite only these):\n"
        + "\n".join(f"  - {u}" for u in sorted(allowed_urls))
        + "\n\nWrite the brief now. Markdown only — no JSON, no code blocks."
    )
    raw = llm(_PROSE_SYSTEM, user_prompt)
    if not raw or len(raw.strip()) < 200:
        return "", []
    cleaned, stripped = _strip_ghost_urls(raw, allowed_urls)
    return cleaned, stripped


# ── Deterministic synthesizer ────────────────────────────────────────────────


def _summary_line(cm: ClaimsModel) -> str:
    supported = [c for c in cm.claims.values() if c.status == ClaimStatus.SUPPORTED]
    if not supported:
        return (
            "The investigation did not produce claims at high confidence. "
            "See the Open Questions section for what remains unverified."
        )
    supported.sort(key=lambda c: -c.confidence)
    best = supported[:3]
    return " ".join(c.text.rstrip(".") + "." for c in best)


def synthesize_report(cm: ClaimsModel) -> str:
    """Render the final report straight from the claims model.

    Every citation in this report is a URL that was actually fetched during
    the run (tracked as Evidence on a claim). Confidence is read from the
    claims model, not asserted. Open claims are surfaced honestly.
    """
    supported = sorted(
        [c for c in cm.claims.values() if c.status == ClaimStatus.SUPPORTED],
        key=lambda c: (-c.confidence, -c.priority),
    )
    partial = sorted(
        [c for c in cm.claims.values() if c.status == ClaimStatus.PARTIAL],
        key=lambda c: (-c.confidence, -c.priority),
    )
    refuted = sorted(
        [c for c in cm.claims.values() if c.status == ClaimStatus.REFUTED],
        key=lambda c: -c.priority,
    )
    open_ = sorted(
        [c for c in cm.claims.values()
         if c.status in (ClaimStatus.UNKNOWN, ClaimStatus.INVESTIGATING, ClaimStatus.ABANDONED)],
        key=lambda c: -c.priority,
    )

    budget = cm.budget
    total = len(cm.claims)

    lines: list[str] = []

    lines.append("## Summary")
    lines.append(_summary_line(cm))
    lines.append("")
    lines.append(
        f"**Investigation summary**: {len(supported)} of {total} claims supported, "
        f"{len(partial)} partial, {len(refuted)} refuted, {len(open_)} open. "
        f"Budget used: {budget.fetches_used} fetch(es), "
        f"{budget.searches_used} search(es), "
        f"{budget.llm_calls_used} LLM call(s), "
        f"{time.time() - budget.started_at:.0f}s wall-clock."
    )
    lines.append("")

    def _claim_block(c: Claim) -> list[str]:
        out = [f"### {c.text}",
               f"*Confidence: {c.confidence:.2f}  |  Evidence: "
               f"{len(c.support)} supporting, {len(c.contradictions)} contradicting*",
               ""]
        for ev in c.support:
            out.append(f"> \"{ev.quote}\"")
            out.append(f">")
            out.append(f"> — {ev.url}")
            out.append("")
        for ev in c.contradictions:
            out.append(f"> ⚠ Contradicted by: \"{ev.quote}\"")
            out.append(f"> — {ev.url}")
            out.append("")
        return out

    if supported:
        lines.append("## Supported findings")
        lines.append("")
        for c in supported:
            lines.extend(_claim_block(c))

    if partial:
        lines.append("## Partial findings")
        lines.append("Evidence points in one direction but isn't strong enough to call the "
                     "claim supported.")
        lines.append("")
        for c in partial:
            lines.extend(_claim_block(c))

    if refuted:
        lines.append("## Refuted claims")
        lines.append("")
        for c in refuted:
            lines.extend(_claim_block(c))

    if open_:
        lines.append("## Open questions (not resolved this run)")
        lines.append("These were investigated but no supporting primary source was found "
                     "within the budget. They are reported honestly rather than fabricated.")
        lines.append("")
        for c in open_:
            attempts = f" (tried {c.attempts}×)" if c.attempts else ""
            lines.append(f"- **{c.text}**{attempts} — still unresolved")
        lines.append("")

    # Fetched sources — the canonical list of URLs that influenced this report
    fetched = sorted(cm.fetched_urls)
    if fetched:
        lines.append("## Sources fetched")
        for i, url in enumerate(fetched, 1):
            lines.append(f"{i}. {url}")
        lines.append("")

    return "\n".join(lines)


# ── Main loop ────────────────────────────────────────────────────────────────


def run_adaptive(
    query: str,
    depth: str = "medium",
    job_id: Optional[str] = None,
    jobs_dir: Optional[Path] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> tuple[str, ClaimsModel]:
    """Run the adaptive loop. Returns (report_markdown, final_claims_model)."""
    cm = ClaimsModel(query=query, budget=preset_budget(depth))

    # ── Wire observability (reuse existing tools / scratchpad) ─────────
    if job_id and jobs_dir:
        from scratchpad import Scratchpad
        import tools as _tools_module
        import grounding as _grounding
        sp = Scratchpad(job_id, jobs_dir)
        _tools_module.set_stream_emitter(sp.stream_event)
        _tools_module.set_fetch_persister(
            lambda url, title, category, text: _grounding.append_fetched(
                jobs_dir, job_id, url, title, category, text
            )
        )
        _tools_module.set_subject_query(query)
        _log = log_fn if log_fn else (lambda msg, agent="": sp.log(msg, agent=agent))
        _emit = sp.stream_event
    else:
        def _log(msg: str, agent: str = "") -> None:
            prefix = f"[{agent}] " if agent else ""
            print(f"{prefix}{msg}")

        def _emit(event: dict) -> None:
            pass

    # ── LLM caller tracks usage for budget accounting ───────────────────
    def _track() -> None:
        cm.budget.llm_calls_used += 1
    llm = _build_llm_caller(_track)

    # ── Tools ────────────────────────────────────────────────────────────
    from tools import WebSearchTool, FetchPageTool
    from source_classifier import classify as _classify_url
    search_tool = WebSearchTool()
    fetch_tool  = FetchPageTool()

    # ── 1. Decompose ────────────────────────────────────────────────────
    _log(f"Decomposing query into verifiable claims…", agent="Planner")
    initial = decompose_query(query, llm)
    for ic in initial:
        cm.add_claim(ic["text"], priority=ic["priority"])
    if not cm.claims:
        # Fallback so the loop has something to chew on
        cm.add_claim(query, priority=1.0)
    _log(f"Initial claims: {len(cm.claims)}", agent="Planner")
    _emit({"type": "claims_snapshot", "model": cm.to_dict()})
    _emit_plan_panel(cm, _emit)
    _emit_thought(_emit, "Decomposing the research query into verifiable claims",
                  f"{len(cm.claims)} initial claims set; loop will investigate each in priority order.")
    for c in cm.claims.values():
        _log(f"  • ({c.priority:.2f}) {c.text}", agent="Planner")

    # ── 2. Main loop ────────────────────────────────────────────────────
    consecutive_unproductive = 0
    consecutive_searches = 0                  # tactical circuit-breaker counter
    # Strategist controls — replace stop-on-stagnation with re-plan-on-stagnation.
    PERIODIC_STRATEGIST_INTERVAL = 5          # also re-plan every N tactical loops
    STAGNATION_TRIGGER = 3                    # consecutive unproductive turns → strategist
    MAX_STRATEGIST_TURNS = 5                  # per-run hard cap on meta-loop calls
    strategist_turns = 0
    strategist_zero_change_streak = 0
    loops_since_last_strategist = 0
    # URLs discovered by searches but not yet fetched. Entries are
    # {"url", "title", "snippet"}; de-duped by URL. The planner sees
    # this list so it can propose fetches naturally.
    surfaced_urls: list[dict] = []
    _seen_surfaced: set[str] = set()

    while True:
        # ── Hard stops only — stagnation no longer ends the run ─────────
        reason = cm.budget.exhausted()
        if reason:
            _log(f"Stopping — budget exhausted: {reason}.", agent="Planner")
            break
        if cm.is_satisfied():
            _log("Stopping — claims sufficiently supported.", agent="Planner")
            break
        if strategist_zero_change_streak >= 2:
            _log("Stopping — strategist has no further suggestions (2 zero-change turns).",
                 agent="Strategist")
            break

        cm.budget.loops_used += 1
        # Drop already-fetched URLs from the surfaced queue so the
        # planner isn't tempted to refetch.
        surfaced_urls = [s for s in surfaced_urls if s["url"] not in cm.fetched_urls]

        # ── Decide: tactical turn or strategist turn? ───────────────────
        # Strategist runs on stagnation OR periodically OR when we've
        # never run one yet but tactical loop is asking to stop.
        needs_strategist = (
            (consecutive_unproductive >= STAGNATION_TRIGGER)
            or (loops_since_last_strategist >= PERIODIC_STRATEGIST_INTERVAL
                and cm.budget.loops_used > 1)
        )

        if needs_strategist and strategist_turns < MAX_STRATEGIST_TURNS:
            strategist_turns += 1
            loops_since_last_strategist = 0
            replan = strategic_replan(cm, llm, available_urls=surfaced_urls)
            _log(
                f"🎯 Strategist (turn {strategist_turns}/{MAX_STRATEGIST_TURNS}): "
                f"{replan['diagnosis'] or '(no diagnosis given)'}",
                agent="Strategist",
            )
            # Apply plan revisions
            changes = 0
            for upd in replan.get("priority_updates", []):
                cid = upd["claim_id"]
                old_p = cm.claims[cid].priority
                cm.claims[cid].priority = upd["new_priority"]
                _log(f"  priority: {cm.claims[cid].text[:60]}  {old_p:.2f} → {upd['new_priority']:.2f}",
                     agent="Strategist")
                changes += 1
            for cid in replan.get("abandon", []):
                if cid in cm.claims and not cm.claims[cid].status.terminal:
                    cm.claims[cid].abandon()
                    _log(f"  abandoned: {cm.claims[cid].text[:80]}", agent="Strategist")
                    changes += 1
            for nc in replan.get("new_claims", []):
                new = cm.add_claim(nc["text"], priority=nc["priority"])
                _log(f"  new claim ({new.priority:.2f}): {new.text}", agent="Strategist")
                changes += 1
            if changes == 0:
                strategist_zero_change_streak += 1
            else:
                strategist_zero_change_streak = 0
            _emit({"type": "strategist", "diagnosis": replan["diagnosis"],
                   "priority_updates": replan.get("priority_updates", []),
                   "abandoned": replan.get("abandon", []),
                   "new_claim_count": len(replan.get("new_claims", [])),
                   "next_action_type": (replan.get("next_action") or {}).get("type", "")})
            _emit({"type": "claims_snapshot", "model": cm.to_dict()})
            _emit_plan_panel(cm, _emit)
            if replan["diagnosis"]:
                _emit_thought(
                    _emit,
                    f"Strategist re-plan ({changes} changes)",
                    replan["diagnosis"],
                )
            # The strategist's recommended next action drives this loop turn.
            action = replan["next_action"] or {"type": "stop", "reason": "strategist returned no action"}
            consecutive_unproductive = 0
        else:
            # ── Tactical turn ──────────────────────────────────────────
            loops_since_last_strategist += 1
            action = next_action(cm, llm, available_urls=surfaced_urls)
            # Circuit-breaker: if the planner has proposed three searches in
            # a row and there are surfaced URLs waiting to be fetched, force
            # a fetch of the most promising available URL.
            if (
                action["type"] == "search"
                and consecutive_searches >= 2
                and surfaced_urls
            ):
                forced = surfaced_urls[0]
                _log(
                    f"Circuit-breaker: 3 searches without a fetch → forcing fetch of "
                    f"{forced['url']}",
                    agent="Planner",
                )
                action = {
                    "type": "fetch",
                    "url":  forced["url"],
                    "target_claim_id": action.get("target_claim_id"),
                }

        if action["type"] == "stop":
            _log(f"Planner chose to stop: {action.get('reason','')}", agent="Planner")
            break

        target_id = action.get("target_claim_id")
        if target_id and target_id in cm.claims:
            cm.claims[target_id].attempts += 1

        # ── 2b. Execute action ─────────────────────────────────────────
        t0 = time.time()
        if action["type"] == "search":
            q = action["query"]
            _log(f"🔎 Search: {q}", agent="Researcher")
            try:
                result = search_tool._run(query=q)
            except Exception as exc:
                _log(f"Search failed: {exc}", agent="Researcher")
                consecutive_unproductive += 1
                cm.log_action(action, f"(search failed: {exc})")
                continue
            cm.budget.searches_used += 1
            consecutive_searches += 1
            # Harvest URLs so the planner can propose fetches next turn.
            harvested = _harvest_search_urls(result)
            new_count = 0
            for item in harvested:
                if item["url"] in _seen_surfaced or item["url"] in cm.fetched_urls:
                    continue
                _seen_surfaced.add(item["url"])
                surfaced_urls.append(item)
                new_count += 1
            if new_count:
                _log(f"  surfaced {new_count} new URL(s) to fetch queue", agent="Researcher")
            evidence_kind = "search"
            evidence_url = ""
            evidence_cat = ""
        elif action["type"] == "fetch":
            url = action["url"]
            if url in cm.fetched_urls:
                _log(f"(skip duplicate fetch: {url})", agent="Researcher")
                consecutive_unproductive += 1
                cm.log_action(action, "(duplicate fetch skipped)")
                continue
            _log(f"📄 Fetch: {url}", agent="Researcher")
            try:
                result = fetch_tool._run(url=url)
            except Exception as exc:
                _log(f"Fetch failed: {exc}", agent="Researcher")
                consecutive_unproductive += 1
                cm.log_action(action, f"(fetch failed: {exc})")
                continue
            cm.budget.fetches_used += 1
            cm.fetched_urls.add(url)
            consecutive_searches = 0   # reset circuit-breaker on a fetch
            evidence_kind = "fetch"
            evidence_url = url
            evidence_cat = _classify_url(url)
        else:
            consecutive_unproductive += 1
            cm.log_action(action, f"(unknown action type: {action['type']})")
            continue

        elapsed = time.time() - t0
        cm.log_action(action, f"{elapsed:.1f}s; result {len(result)} chars")

        # ── 2c. Evaluator integrates the result ────────────────────────
        upd = integrate_result(cm, evidence_kind, result, evidence_url, evidence_cat, llm,
                               subject=cm.query)
        any_progress = bool(upd.get("updated_claims") or upd.get("new_claim_ids"))
        if any_progress:
            consecutive_unproductive = 0
        else:
            consecutive_unproductive += 1

        # Stream + log the update
        _emit({
            "type":            "claims_update",
            "evidence_kind":   evidence_kind,
            "evidence_url":    evidence_url,
            "updated_claims":  upd.get("updated_claims", []),
            "new_claim_ids":   upd.get("new_claim_ids", []),
            "parse_failed":    upd.get("parse_failed", False),
            "budget":          cm.budget.snapshot(),
        })
        # Push panel updates so Plan / Notes refresh live with each evidence pass.
        if any_progress:
            updated_ids = [u["id"] for u in upd.get("updated_claims", []) if u.get("support_count", 0) > 0]
            _emit_notes_panel(cm, _emit, updated_ids)
            _emit_plan_panel(cm, _emit)
        _log(
            f"Evaluator: updated {len(upd.get('updated_claims', []))} claim(s), "
            f"added {len(upd.get('new_claim_ids', []))} new. "
            f"{cm.summary_line()}",
            agent="Evaluator",
        )

    # ── 3. Snapshot final state + synthesize ────────────────────────────
    _emit({"type": "claims_snapshot", "model": cm.to_dict()})
    _emit_plan_panel(cm, _emit)

    _log("Rendering final report…", agent="Synthesizer")

    # Deterministic synthesis is always available — both the appendix below
    # and the fallback if the prose polish fails.
    deterministic = synthesize_report(cm)

    # LLM polish pass — write a flowing narrative grounded in the claims
    # model's evidence. Falls back to deterministic-only on any failure.
    prose, stripped_urls = synthesize_prose(cm, llm)
    if prose.strip():
        if stripped_urls:
            _log(f"Prose synthesis: stripped {len(stripped_urls)} ghost URL(s) "
                 f"not in evidence set", agent="Synthesizer")
        report = (
            prose
            + "\n\n---\n\n## Verification Appendix\n\n"
            + "_The brief above is grounded in the evidence below — every URL "
              "cited in the prose was actually fetched during this run, and "
              "every quoted snippet is verbatim from the source page. The "
              "structured breakdown lets you audit any specific claim._\n\n"
            + deterministic
        )
        _emit_thought(_emit, "Prose synthesis complete",
                      f"Narrative + verification appendix; "
                      f"{len(stripped_urls)} ghost URLs stripped." if stripped_urls
                      else "Narrative + verification appendix.")
    else:
        _log("Prose synthesis unavailable — falling back to deterministic structure.",
             agent="Synthesizer")
        report = deterministic

    # Update the Draft panel with the final report so users can review there too.
    try:
        _emit({"type": "draft_update", "content": report})
    except Exception:
        pass

    # Persist the claims model alongside the report for inspection
    if job_id and jobs_dir:
        try:
            (jobs_dir / f"{job_id}.claims.json").write_text(
                json.dumps(cm.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    return report, cm


# ── Standalone CLI entry point ──────────────────────────────────────────────


def main_cli(query: str, depth: str = "medium") -> int:
    """Run adaptive mode from the CLI, print report to stdout, save to reports/."""
    print("─" * 60)
    print(f"Adaptive research: {query}")
    print(f"Depth preset: {depth}")
    print("─" * 60)
    report, cm = run_adaptive(query, depth=depth)
    print()
    print(report)
    print()
    print("─" * 60)
    print(cm.summary_line())

    # Best-effort save
    try:
        from datetime import datetime
        reports_dir = Path(__file__).parent / "reports"
        reports_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", query)[:60].strip("_")
        prefix = f"{ts}_adaptive_{slug}"
        (reports_dir / f"{prefix}.md").write_text(
            f"# Query\n{query}\n\n{report}", encoding="utf-8"
        )
        art_dir = reports_dir / prefix
        art_dir.mkdir(exist_ok=True)
        (art_dir / "claims.json").write_text(
            json.dumps(cm.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Saved to reports/{prefix}.md")
    except OSError as exc:
        print(f"(could not save: {exc})")

    return 0
