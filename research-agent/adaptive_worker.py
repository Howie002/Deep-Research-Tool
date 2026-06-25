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
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from claims import Claim, ClaimsModel, ClaimStatus, Evidence, preset_budget
from config import LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
from telemetry_report import report_from_response, report_usage
from adaptive_planner import decompose_query, next_action, strategic_replan
from adaptive_evaluator import apply_evaluation, evaluate_result, integrate_result


# ── Concurrency (batched-round fetch + evaluate) ─────────────────────────────
# When enabled, after the planner picks a fetch the executor drains up to
# (batch-1) additional already-queued URLs and fetches + evaluates them in
# parallel threads. The eval LLM calls hit the proxy concurrently; applying
# results to the claims model happens serially on the main thread.
#
# DEFAULT OFF (decision 2026-06-05): on a single shared Nano the eval calls are
# prefill-bound, so concurrency only buys ~4% (fetch-I/O overlap) while adding
# GPU contention for other aisandbox users. Kept flag-ready — flip on once
# Death Star is serving (multi-GPU spreads the prefill via least-busy routing).
# Toggle / A-B via env:
#   DR_CONCURRENCY=1   → enable batched-round fetch + evaluate
#   DR_BATCH_SIZE=<n>  → override the per-depth default batch size
_CONCURRENCY_ENABLED = os.getenv("DR_CONCURRENCY", "0").strip().lower() in ("1", "true", "yes", "on")
_DEPTH_BATCH_DEFAULT = {"light": 3, "medium": 5, "heavy": 8, "ultra": 8}


def _resolve_batch_size(depth: str) -> int:
    env = os.getenv("DR_BATCH_SIZE", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return _DEPTH_BATCH_DEFAULT.get((depth or "").lower(), 5)


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


def _build_llm_caller(track_fn: Callable[[], None]) -> Callable[..., Optional[str]]:
    """Return an (system, user, max_tokens=...) -> content callable that tracks usage.

    track_fn is called every time a request is ISSUED (success or failure),
    so budget accounting works even if LM Studio errors. The optional
    `max_tokens` lets the prose synthesis call ask for more headroom than
    the default short-prompt budget.
    """
    def call(system: str, user: str, *, max_tokens: int = 1200) -> Optional[str]:
        track_fn()
        payload = {
            "model": LM_STUDIO_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "presence_penalty": 0.1,
            "frequency_penalty": 0.3,
            # Disable reasoning-model "thinking" so the model's chain-of-thought
            # doesn't leak into the response content. Some LM Studio models
            # ignore this; combined with the post-pass stripper below it's
            # belt-and-suspenders against scaffolding bleed-through.
            "enable_thinking": False,
            "reasoning": False,
            # repetition / repeat penalty — Gemma in particular needs this
            # to avoid token-loop collapse on long outputs.
            "repetition_penalty": 1.15,
            "repeat_penalty": 1.15,
        }
        req = urllib.request.Request(
            LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
            method="POST",
        )
        _t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            # Cross-tool usage telemetry (best-effort) — this is the adaptive
            # engine's core LLM caller, so it covers the bulk of a research run.
            report_from_response(
                body, LM_STUDIO_MODEL, "research",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )
            msg = body.get("choices", [{}])[0].get("message", {}) or {}
            return (msg.get("content") or msg.get("reasoning_content") or "").strip() or None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
            report_usage(
                model=LM_STUDIO_MODEL, feature="research",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
                status="error",
            )
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


def _emit_final_notes(cm: ClaimsModel, emit) -> None:
    """At end-of-run, emit one curated takeaway note per claim with evidence.

    Each note is a structured takeaway (not a raw quote dump):
      - The claim itself, phrased as a takeaway statement
      - A one-line synthesis based on the supporting evidence
      - Each unique source URL with its supporting quote underneath
      - Tension/contradiction notes if any sources disagreed

    This replaces the earlier per-evidence emission which produced one note
    per Evidence object — that left the Notes tab as a noisy duplicate-laden
    dump rather than a curated synthesis.
    """
    # Order: supported first (highest conf), then partial, then refuted/abandoned.
    order_keys = {
        "supported":    0,
        "partial":      1,
        "investigating": 2,
        "refuted":      3,
        "abandoned":    4,
        "unknown":      5,
    }
    claims = sorted(
        [c for c in cm.claims.values() if c.support or c.contradictions],
        key=lambda c: (order_keys.get(c.status.value, 9), -c.confidence, -c.priority),
    )
    for c in claims:
        # Status badge + confidence
        status_badge = {
            "supported":     "✓ supported",
            "partial":       "◐ partial",
            "investigating": "↻ investigating",
            "refuted":       "✗ refuted",
            "abandoned":     "— abandoned",
            "unknown":       "□ open",
        }.get(c.status.value, c.status.value)

        # Build a single takeaway block per claim.
        lines = [
            f"**{c.text}**",
            f"_{status_badge}  ·  confidence {c.confidence:.2f}  ·  "
            f"{len(c.support)} supporting / {len(c.contradictions)} contradicting_",
            "",
        ]

        # Group supporting quotes by URL so duplicates collapse.
        seen_urls: set[str] = set()
        for ev in c.support:
            if ev.url in seen_urls:
                continue
            seen_urls.add(ev.url)
            lines.append(f"> \"{ev.quote}\"")
            lines.append(f">")
            lines.append(f"> — [{ev.url}]({ev.url})")
            lines.append("")

        if c.contradictions:
            lines.append("**Sources in tension:**")
            for ev in c.contradictions:
                lines.append(f"- \"{ev.quote}\" — [{ev.url}]({ev.url})")
            lines.append("")

        try:
            emit({
                "type":    "note_add",
                "content": "\n".join(lines).rstrip(),
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


def _trim_prose_scaffolding(raw: str) -> str:
    """Strip chain-of-thought / planning scaffolding the model emits before
    the actual brief.

    Reasoning-tuned local models (Gemma 4 -a4b, etc.) often write a planning
    block — "Subject: ...", "Goal: ...", "Constraint Check: ...", "Drafting
    Final Version:" — before the final answer. We want only the answer.

    Strategy: locate the first ``## Summary`` heading and discard everything
    before it. If no summary heading exists, fall back to whatever the model
    produced after the first H2 heading. If neither exists, return raw.
    """
    if not raw:
        return raw
    # Prefer the first '## Summary' heading.
    m = re.search(r"^\s*##\s*Summary\b", raw, flags=re.IGNORECASE | re.MULTILINE)
    if m:
        return raw[m.start():].strip()
    # Fallback: any ## heading.
    m = re.search(r"^\s*##\s+\S", raw, flags=re.MULTILINE)
    if m:
        return raw[m.start():].strip()
    return raw.strip()


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
        + "\n\nWrite the brief now. Begin DIRECTLY with `## Summary` — do not "
          "write any preamble, planning notes, scaffolding, or self-correction "
          "before the report itself. Markdown only — no JSON, no code blocks."
    )
    raw = llm(_PROSE_SYSTEM, user_prompt, max_tokens=2400)
    if not raw or len(raw.strip()) < 200:
        return "", []
    # Strip any scaffolding the model leaked before the actual brief.
    trimmed = _trim_prose_scaffolding(raw)
    cleaned, stripped = _strip_ghost_urls(trimmed, allowed_urls)
    return cleaned, stripped


# ── Deterministic synthesizer ────────────────────────────────────────────────


def _norm_url_key(u: str) -> str:
    """Loose URL key for dedup: lowercase host/scheme, drop www, fragment,
    tracking params and trailing slash. Mirrors the loop's `_norm_url` but is
    module-level so the renderer can reuse it."""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        s = urlsplit(u.strip())
        scheme = (s.scheme or "https").lower()
        host = s.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        host = host.split(":")[0] if host.endswith((":80", ":443")) else host
        path = s.path.rstrip("/")
        q = [(k, v) for k, v in parse_qsl(s.query)
             if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
        return urlunsplit((scheme, host, path, urlencode(q), ""))
    except Exception:
        return u.strip().lower().rstrip("/")


def _dedup_evidence(evs: list[Evidence]) -> list[Evidence]:
    """Drop duplicate quotes within one claim block. Two pieces of evidence are
    the same if their normalized URL AND normalized quote match. Keeps order
    (first occurrence wins)."""
    seen: set[tuple[str, str]] = set()
    out: list[Evidence] = []
    for ev in evs:
        qkey = re.sub(r"\s+", " ", (ev.quote or "")).strip().lower()
        key = (_norm_url_key(ev.url), qkey)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


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
        sup = _dedup_evidence(c.support)
        con = _dedup_evidence(c.contradictions)
        out = [f"### {c.text}",
               f"*Confidence: {c.confidence:.2f}  |  Evidence: "
               f"{len(sup)} supporting, {len(con)} contradicting*",
               ""]
        for ev in sup:
            out.append(f"> \"{ev.quote}\"")
            out.append(f">")
            out.append(f"> — {ev.url}")
            out.append("")
        for ev in con:
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
    clarifications: str = "",
) -> tuple[str, ClaimsModel]:
    """Run the adaptive loop. Returns (report_markdown, final_claims_model).

    `clarifications` is the user's pre-run scoping text from the
    Dynamic Clarifying Questions modal — passed into the decomposition step
    AND threaded into the planner / strategist prompts so the loop stays
    aligned to the user's intent throughout, not just at start.
    """
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
    # _track is called from the concurrent evaluate_result() threads, so the
    # counter increment must be guarded.
    _track_lock = threading.Lock()
    def _track() -> None:
        with _track_lock:
            cm.budget.llm_calls_used += 1
    llm = _build_llm_caller(_track)

    # Concurrency setup for this run (batched-round fetch + evaluate).
    _batch_size = _resolve_batch_size(depth)
    if _CONCURRENCY_ENABLED and _batch_size > 1:
        _log(f"Concurrency ON — fetch+evaluate batch size {_batch_size}", agent="Planner")

    # ── Tools ────────────────────────────────────────────────────────────
    from tools import WebSearchTool, FetchPageTool
    from source_classifier import classify as _classify_url
    search_tool = WebSearchTool()
    fetch_tool  = FetchPageTool()

    # ── 1. Decompose ────────────────────────────────────────────────────
    _log(f"Decomposing query into verifiable claims…", agent="Planner")
    initial = decompose_query(query, llm, clarifications=clarifications)
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
    _seen_surfaced: set[str] = set()        # normalized-URL dedup keys (surfaced queue)
    _fetched_norm: set[str] = set()         # normalized-URL dedup keys (already fetched)

    def _norm_url(u: str) -> str:
        """Normalize a URL for dedup — lowercase scheme/host, drop www / default port /
        fragment / tracking params, strip trailing slash. Collapses near-duplicate
        surfaced URLs (e.g. trailing-slash variants) so they don't waste fetch budget."""
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        try:
            s = urlsplit((u or "").strip())
            scheme = (s.scheme or "http").lower()
            host = (s.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            netloc = host
            if s.port and not ((scheme == "http" and s.port == 80) or (scheme == "https" and s.port == 443)):
                netloc = f"{host}:{s.port}"
            keep = [(k, v) for k, v in parse_qsl(s.query)
                    if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_", "ref"))]
            return urlunsplit((scheme, netloc, s.path.rstrip("/"), urlencode(sorted(keep)), ""))
        except Exception:
            return (u or "").strip().rstrip("/").lower()

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
        surfaced_urls = [s for s in surfaced_urls if _norm_url(s["url"]) not in _fetched_norm]

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
            replan = strategic_replan(cm, llm, available_urls=surfaced_urls,
                                      clarifications=clarifications)
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
            action = next_action(cm, llm, available_urls=surfaced_urls,
                                 clarifications=clarifications)
            # Circuit-breaker: searches only PRODUCE URLs — fetches produce the
            # evidence that resolves claims. As soon as the planner proposes a
            # SECOND consecutive search while URLs are already waiting, force a
            # fetch of the most promising one. Tuned down from 3→2 consecutive
            # searches because runs were ~4:1 search:fetch (30 searches / 7
            # fetches) and starving claim resolution — fetch what you found
            # before searching for more.
            if (
                action["type"] == "search"
                and consecutive_searches >= 1
                and surfaced_urls
            ):
                forced = surfaced_urls[0]
                _log(
                    f"Circuit-breaker: search proposed with URLs already queued → "
                    f"forcing fetch of {forced['url']}",
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
                nu = _norm_url(item["url"])
                if nu in _seen_surfaced or nu in _fetched_norm:
                    continue
                _seen_surfaced.add(nu)
                surfaced_urls.append(item)
                new_count += 1
            if new_count:
                _log(f"  surfaced {new_count} new URL(s) to fetch queue", agent="Researcher")
            evidence_kind = "search"
            evidence_url = ""
            evidence_cat = ""
        elif action["type"] == "fetch":
            url = action["url"]
            if _norm_url(url) in _fetched_norm:
                _log(f"(skip duplicate fetch: {url})", agent="Researcher")
                consecutive_unproductive += 1
                cm.log_action(action, "(duplicate fetch skipped)")
                continue

            # ── Build the fetch batch ──────────────────────────────────────
            # Planner's chosen URL first, then drain additional already-queued
            # surfaced URLs (independent fetches) up to the batch size, clamped
            # to remaining fetch budget. Concurrency off → batch of 1 (the
            # original sequential path, unchanged).
            batch_urls = [url]
            if _CONCURRENCY_ENABLED and _batch_size > 1:
                remaining = max(1, cm.budget.max_fetches - cm.budget.fetches_used)
                cap = min(_batch_size, remaining)
                for s in surfaced_urls:
                    if len(batch_urls) >= cap:
                        break
                    su = s.get("url", "")
                    if not su or su in batch_urls or _norm_url(su) in _fetched_norm:
                        continue
                    batch_urls.append(su)

            if len(batch_urls) == 1:
                # ── Single fetch (sequential path) ─────────────────────────
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
                _fetched_norm.add(_norm_url(url))
                consecutive_searches = 0   # reset circuit-breaker on a fetch
                evidence_kind = "fetch"
                evidence_url = url
                evidence_cat = _classify_url(url)
                # falls through to the common single-result integrate below
            else:
                # ── Concurrent batched fetch + evaluate ────────────────────
                # Each worker thread does fetch (web I/O) → evaluate_result
                # (LLM call, PURE — no cm mutation). The eval calls hit the
                # proxy concurrently → vLLM batches them. Results are then
                # applied to the claims model SERIALLY on this thread.
                _log(f"📄 Fetch batch ({len(batch_urls)}) — concurrent:", agent="Researcher")
                for bu in batch_urls:
                    _log(f"   • {bu}", agent="Researcher")
                consecutive_searches = 0

                def _fetch_and_eval(u: str):
                    try:
                        text = fetch_tool._run(url=u)
                    except Exception as exc:
                        return (u, None, None, f"fetch failed: {exc}")
                    parsed = evaluate_result(cm, "fetch", text, u, llm, subject=cm.query)
                    return (u, text, parsed, None)

                t_batch = time.time()
                results: list[tuple] = []
                with ThreadPoolExecutor(max_workers=len(batch_urls)) as _ex:
                    _futs = {_ex.submit(_fetch_and_eval, u): u for u in batch_urls}
                    for _fut in as_completed(_futs):
                        try:
                            results.append(_fut.result())
                        except Exception as exc:
                            results.append((_futs[_fut], None, None, f"worker error: {exc}"))
                batch_elapsed = time.time() - t_batch

                # ── Serial merge: apply each parsed verdict to the model ────
                batch_progress = False
                applied_n = 0
                for (u, text, parsed, err) in results:
                    if err or text is None:
                        _log(f"Fetch failed: {u} ({err})", agent="Researcher")
                        continue
                    cm.budget.fetches_used += 1
                    cm.fetched_urls.add(u)
                    _fetched_norm.add(_norm_url(u))
                    cm.log_action({"type": "fetch", "url": u}, f"{len(text)} chars (batched)")
                    cat = _classify_url(u)
                    upd = apply_evaluation(cm, parsed, "fetch", u, cat)
                    applied_n += 1
                    if upd.get("updated_claims") or upd.get("new_claim_ids"):
                        batch_progress = True
                    _emit({
                        "type":           "claims_update",
                        "evidence_kind":  "fetch",
                        "evidence_url":   u,
                        "updated_claims": upd.get("updated_claims", []),
                        "new_claim_ids":  upd.get("new_claim_ids", []),
                        "parse_failed":   upd.get("parse_failed", False),
                        "budget":         cm.budget.snapshot(),
                    })
                if batch_progress:
                    consecutive_unproductive = 0
                    _emit_plan_panel(cm, _emit)
                else:
                    consecutive_unproductive += 1
                _log(
                    f"Fetch batch: applied {applied_n}/{len(batch_urls)} pages in "
                    f"{batch_elapsed:.1f}s. {cm.summary_line()}",
                    agent="Evaluator",
                )
                continue   # batch fully handled — skip the common single-result integrate
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
        # Push live Plan-panel update each evidence pass (Notes are emitted
        # as a curated synthesis at end-of-run, not noisily per-evidence).
        if any_progress:
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
    # Curated takeaway notes — one per claim with evidence, deduped by URL.
    _emit_final_notes(cm, _emit)

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
