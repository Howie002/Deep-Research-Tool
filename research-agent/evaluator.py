"""
evaluator.py — Heuristic run evaluator.

Reads a run's meta.json (and optionally audit.jsonl) and produces:
  - A 0–100 numeric score
  - A letter grade (A–F)
  - A list of actionable suggestions, each with a severity level
  - A brief summary of key stats

Called by the /api/reports/{filename}/evaluate endpoint. Designed to be
fast (no LLM calls) and deterministic so results are consistent across
repeated evaluations of the same run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Severity levels ────────────────────────────────────────────────────────────
CRITICAL = "critical"   # Likely broken run, score −25 to −30
WARNING  = "warning"    # Suboptimal but functional, score −8 to −15
INFO     = "info"       # Minor improvement opportunity, score −3 to −5
GOOD     = "good"       # Positive observation, no score deduction


def evaluate_run(meta: dict, audit_events: list[dict] | None = None) -> dict:
    """
    Analyse a completed run and return a structured evaluation dict:
        {
          score:       int   (0–100),
          grade:       str   ("A"–"F"),
          suggestions: list[{severity, title, detail}],
          stats:       dict  (key numbers for the summary card),
        }
    """
    suggestions: list[dict] = []
    score = 100

    def flag(severity: str, title: str, detail: str, deduct: int = 0) -> None:
        suggestions.append({"severity": severity, "title": title, "detail": detail})
        nonlocal score
        score -= deduct

    # ── 1. Run health ──────────────────────────────────────────────────────────
    status = meta.get("status", "")
    if status == "error":
        flag(CRITICAL,
             "Run ended with an error",
             "The pipeline failed before producing a report. Check the audit log "
             "for the error message and ensure LM Studio is reachable.",
             deduct=30)

    # ── 2. Report length ───────────────────────────────────────────────────────
    word_count = meta.get("word_count", 0)
    if word_count < 80:
        flag(CRITICAL,
             f"Report is extremely short ({word_count} words)",
             "The synthesizer produced almost nothing. This usually means the "
             "pipeline failed mid-way or the LLM hit a context or token limit.",
             deduct=25)
    elif word_count < 250:
        flag(WARNING,
             f"Report is brief ({word_count} words)",
             "A thorough report typically exceeds 400 words. Consider asking "
             "the synthesizer for more detail or increasing max_iter.",
             deduct=10)
    elif word_count >= 600:
        flag(GOOD,
             f"Comprehensive report ({word_count} words)",
             "The synthesizer produced a substantive report.")

    # ── 3. Source coverage ─────────────────────────────────────────────────────
    fetch_count    = meta.get("fetch_count", 0)
    unique_urls    = meta.get("unique_url_count", 0)

    if fetch_count == 0:
        flag(CRITICAL,
             "No pages were read",
             "FetchPageTool was never called. Verify the tool is wired to the "
             "researcher agent and that the task prompt instructs fetching.",
             deduct=30)
    elif fetch_count < 3:
        flag(WARNING,
             f"Very few pages read ({fetch_count})",
             "Aim for at least 5 fetched pages per research run. Consider "
             "increasing MAX_SEARCH_RESULTS or broadening search queries.",
             deduct=15)
    elif fetch_count < 6:
        flag(INFO,
             f"Moderate page coverage ({fetch_count} pages)",
             "Research could go deeper. 8–15 fetched pages typically yields "
             "richer, better-cited reports.",
             deduct=5)
    else:
        flag(GOOD,
             f"Good source coverage ({fetch_count} pages read)",
             f"{unique_urls} unique URLs encountered across all searches.")

    # ── 4. Search diversity ────────────────────────────────────────────────────
    search_count       = meta.get("search_count", 0)
    unique_query_count = meta.get("unique_query_count", 0)

    if search_count == 0:
        flag(CRITICAL,
             "No searches were made",
             "WebSearchTool was never called. Check that the tool is in the "
             "researcher's toolset.",
             deduct=25)
    elif search_count > 0:
        if unique_query_count < 4:
            flag(WARNING,
                 f"Low query diversity ({unique_query_count} unique queries)",
                 "The task prompt asks for at least 4 different search angles. "
                 "If the LLM is re-using similar queries, the _QueryTracker warning "
                 "should help — check the audit log for 'near-duplicate' warnings.",
                 deduct=10)
        dup_ratio = (search_count - unique_query_count) / search_count if search_count else 0
        if dup_ratio > 0.35:
            dup_count = search_count - unique_query_count
            flag(WARNING,
                 f"{dup_count} duplicate search queries ({int(dup_ratio * 100)}%)",
                 "A high proportion of searches were near-identical. This wastes "
                 "iterations and context. The _QueryTracker in tools.py should flag "
                 "these — verify it is active.",
                 deduct=8)

    # ── 5. Planning ────────────────────────────────────────────────────────────
    plan_updates = meta.get("plan_updates", 0)
    if plan_updates == 0:
        flag(WARNING,
             "Researcher never updated the plan",
             "UpdatePlanTool was not called. Step 0 of the research task prompt "
             "explicitly requires calling update_plan. The model may be skipping "
             "tool calls — try lowering temperature or simplifying the prompt.",
             deduct=8)
    elif plan_updates == 1:
        flag(INFO,
             "Plan set once but never revised",
             "Updating the plan as research evolves helps the analyst and "
             "synthesizer understand what was covered.",
             deduct=2)
    else:
        flag(GOOD,
             f"Plan updated {plan_updates} times",
             "The researcher actively tracked its strategy throughout the run.")

    # ── 6. Note-taking ─────────────────────────────────────────────────────────
    note_count = meta.get("note_count", 0)
    if note_count == 0:
        flag(WARNING,
             "No notes were captured",
             "AddNoteTool was never called. Notes feed key findings to the "
             "analyst and synthesizer — without them the later stages work from "
             "raw LLM context alone, which is less reliable.",
             deduct=8)
    elif note_count < 3:
        flag(INFO,
             f"Sparse note-taking ({note_count} note{'s' if note_count != 1 else ''})",
             "Aim for one note per significant finding or source. More structured "
             "notes generally produce better final reports.",
             deduct=3)
    else:
        flag(GOOD,
             f"Active note-taking ({note_count} notes captured)",
             "Key findings were systematically recorded throughout the run.")

    # ── 7. Draft building ──────────────────────────────────────────────────────
    draft_updates = meta.get("draft_updates", 0)
    if draft_updates == 0:
        flag(INFO,
             "No working draft was built",
             "UpdateDraftTool was never called. An incremental draft gives the "
             "synthesizer a starting point and often improves final report quality.",
             deduct=4)

    # ── 8. Source quality ──────────────────────────────────────────────────────
    categories: dict[str, int] = meta.get("source_categories", {})
    if categories:
        total = sum(categories.values())
        if total > 0:
            social  = sum(v for k, v in categories.items() if "Social" in k or "UGC" in k)
            academic = sum(v for k, v in categories.items() if "Academic" in k)
            gov     = sum(v for k, v in categories.items() if "Government" in k or "Gov" in k)

            if social / total > 0.5:
                flag(WARNING,
                     f"High social/UGC source ratio ({social}/{total} sources)",
                     "Social media and user-generated content are lower reliability. "
                     "The analyst prompt asks to seek academic/government sources when "
                     "only social sources exist — check if it acted on this.",
                     deduct=8)
            elif social / total > 0.3:
                flag(INFO,
                     f"Moderate social/UGC proportion ({social}/{total} sources)",
                     "Consider whether key claims are corroborated by higher-tier sources.",
                     deduct=3)

            if academic > 0 or gov > 0:
                flag(GOOD,
                     f"Primary sources found ({academic} academic, {gov} government)",
                     "High-credibility sources were included in the research.")

            if len(categories) == 1:
                cat_name = list(categories.keys())[0]
                flag(INFO,
                     f"All sources from one category ({cat_name})",
                     "Diverse source types improve report reliability. Try adding "
                     "explicit 'site:.edu', 'site:.gov', or 'filetype:pdf' searches.",
                     deduct=4)

    # ── 9. Agent pipeline ──────────────────────────────────────────────────────
    agents_seen = meta.get("agents_seen", [])
    if len(agents_seen) < 2:
        flag(WARNING,
             f"Only {len(agents_seen)} agent stage(s) observed",
             "A full run should pass through all three stages: Research Specialist → "
             "Critical Analyst → Report Synthesizer. The pipeline may have stopped early.",
             deduct=12)

    # ── 10. Elapsed time ───────────────────────────────────────────────────────
    elapsed = meta.get("elapsed_seconds")
    if elapsed:
        if elapsed > 2700:   # 45 min
            flag(INFO,
                 f"Long run time ({elapsed // 60} min {elapsed % 60}s)",
                 "Very long runs often indicate search loops or excessive retries. "
                 "Review the audit log for repeated search patterns.",
                 deduct=3)
        elif elapsed < 30 and fetch_count == 0:
            flag(WARNING,
                 f"Suspiciously fast completion ({elapsed}s with no fetches)",
                 "The pipeline finished very quickly without reading any pages. "
                 "This likely indicates an early error or cancellation.",
                 deduct=5)

    # ── Finalise ───────────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    if score >= 88:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 45:
        grade = "D"
    else:
        grade = "F"

    # Sort: critical first, then warning, info, good
    _order = {CRITICAL: 0, WARNING: 1, INFO: 2, GOOD: 3}
    suggestions.sort(key=lambda s: _order.get(s["severity"], 99))

    return {
        "score": score,
        "grade": grade,
        "suggestions": suggestions,
        "stats": {
            "word_count":        word_count,
            "fetch_count":       fetch_count,
            "search_count":      search_count,
            "unique_queries":    unique_query_count,
            "note_count":        note_count,
            "plan_updates":      plan_updates,
            "draft_updates":     draft_updates,
            "agents_seen":       agents_seen,
            "elapsed_seconds":   elapsed,
            "source_categories": categories,
            "status":            status,
        },
    }


def load_and_evaluate(artifacts_dir: Path) -> dict | None:
    """
    Convenience wrapper: load meta.json from artifacts_dir and evaluate.
    Returns None if meta.json is missing or unreadable.
    """
    meta_file = artifacts_dir / "meta.json"
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    audit_events: list[dict] = []
    audit_file = artifacts_dir / "audit.jsonl"
    if audit_file.exists():
        for line in audit_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    audit_events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    return evaluate_run(meta, audit_events)
