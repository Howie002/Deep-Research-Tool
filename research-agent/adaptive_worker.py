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
from adaptive_planner import decompose_query, next_action
from adaptive_evaluator import integrate_result


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
    for c in cm.claims.values():
        _log(f"  • ({c.priority:.2f}) {c.text}", agent="Planner")

    # ── 2. Main loop ────────────────────────────────────────────────────
    consecutive_unproductive = 0
    while True:
        reason = cm.budget.exhausted()
        if reason:
            _log(f"Stopping — budget exhausted: {reason}.", agent="Planner")
            break
        if cm.is_satisfied():
            _log("Stopping — claims sufficiently supported.", agent="Planner")
            break
        if cm.is_stagnating():
            _log("Stopping — no recent progress.", agent="Planner")
            break
        if consecutive_unproductive >= 4:
            _log("Stopping — 4 consecutive unproductive iterations.", agent="Planner")
            break

        cm.budget.loops_used += 1

        # ── 2a. Planner picks next action ──────────────────────────────
        action = next_action(cm, llm)
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
        upd = integrate_result(cm, evidence_kind, result, evidence_url, evidence_cat, llm)
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
        _log(
            f"Evaluator: updated {len(upd.get('updated_claims', []))} claim(s), "
            f"added {len(upd.get('new_claim_ids', []))} new. "
            f"{cm.summary_line()}",
            agent="Evaluator",
        )

    # ── 3. Snapshot final state + synthesize ────────────────────────────
    _emit({"type": "claims_snapshot", "model": cm.to_dict()})

    _log("Rendering final report…", agent="Synthesizer")
    report = synthesize_report(cm)

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
