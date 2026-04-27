"""
research_worker.py — Standalone research pipeline runner.

Launched as a detached subprocess by mcp_server.py so it survives
MCP server disconnects. Reads the query from a job file, runs the
full CrewAI pipeline, and writes the result back to the same file.

Progress is logged to the job file in real-time via scratchpad.py so
get_research_result() can show the user what is happening while the
pipeline runs.

Usage (internal — called by mcp_server.py):
    python research_worker.py <job_id> <jobs_dir>
"""
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

_job_file: Path | None = None


def _mark_failed(signum=None, frame=None) -> None:
    """Write error status if the process is killed before completing."""
    if _job_file and _job_file.exists():
        try:
            current = json.loads(_job_file.read_text(encoding="utf-8"))
            if current.get("status") == "running":
                current["status"] = "error"
                current["result"] = "Worker process was terminated unexpectedly."
                _job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if signum is not None:
        sys.exit(1)


def _detect_degenerate_output(text: str) -> dict:
    """Return {"degenerate": bool, "signal": str, "sample": str}.

    Catches small-LLM decoding collapse. Three patterns:
      A. Separator-joined token loop: "thess_thess_thess…" or "Corpus-Corpus-…"
      B. Whitespace-separated word loop: 15+ identical consecutive tokens
      C. Low vocabulary diversity in a non-trivially-long response
    """
    if not text:
        return {"degenerate": False, "signal": "", "sample": ""}

    import re as _re
    m = _re.search(r"(\b[\w']{2,20})([\-_])(?:\1\2){8,}", text)
    if m:
        return {"degenerate": True, "signal": "token-loop", "sample": m.group(0)[:160]}

    m = _re.search(r"(\b[\w']{2,20}\b)(?:\s+\1\b){14,}", text)
    if m:
        return {"degenerate": True, "signal": "word-loop", "sample": m.group(0)[:160]}

    if len(text) > 400:
        tokens = _re.findall(r"\b\w+\b", text.lower())
        unique = set(tokens)
        if tokens and len(unique) < 20 and len(tokens) / max(1, len(unique)) > 8:
            return {"degenerate": True, "signal": "low-vocab", "sample": text[:160]}

    return {"degenerate": False, "signal": "", "sample": ""}


def _build_grounding_appendix(gr) -> str:
    """Render a markdown appendix summarising the grounding validator results.

    gr is a grounding.GroundingReport; kept untyped here to avoid importing
    that module at worker-top scope (it pulls in LLM bits we don't need
    before env is set)."""
    tier_badge = {
        "high":      "🟢 HIGH",
        "medium":    "🟡 MEDIUM",
        "low":       "🟠 LOW",
        "very_low":  "🔴 VERY LOW",
    }.get(gr.confidence_tier, gr.confidence_tier.upper())

    lines: list[str] = ["---", "", "## Grounding Audit",
        f"**Computed confidence:** {tier_badge}  (score {gr.confidence_score})",
        "",
        "This section is generated mechanically after synthesis. It replaces the "
        "synthesizer's self-asserted confidence labels — which measure prose "
        "consistency, not source support — with checks against the pages actually "
        "fetched during this run.",
        "",
    ]

    # Pipeline corruption (runtime + structural). Placed first because it
    # indicates the report body may not reflect the research that was done.
    if getattr(gr, "pipeline_corrupted", False):
        lines.append("### 🛑 Pipeline corruption detected")
        lines.append("One or more pipeline stages produced degenerate output or lost context. "
                     "The final report may not reflect the findings in the workspace — check "
                     "`notes.md` and `fetched.jsonl` for the canonical research record.")
        for flag in getattr(gr, "corruption_flags", []) or []:
            agent = flag.get("agent") or "pipeline"
            signal = flag.get("signal") or "unknown"
            sample = (flag.get("sample") or "").replace("\n", " ")[:200]
            lines.append(f"- **{agent}** — {signal}")
            if sample:
                lines.append(f"    - `{sample}`")
        lines.append("")

    # Ghost citations
    if gr.ghost_urls:
        lines.append(f"### ⚠ Ghost citations ({len(gr.ghost_urls)})")
        lines.append("URLs cited in the report that were NOT fetched during this run. "
                     "These may be fabricated or remembered from training data.")
        for u in gr.ghost_urls:
            lines.append(f"- {u}")
        lines.append("")

    # Dead URLs
    if gr.dead_urls:
        lines.append(f"### ⚠ Dead or unreachable URLs ({len(gr.dead_urls)})")
        for u in gr.dead_urls:
            lines.append(f"- {u}")
        lines.append("")

    # Citation verdicts
    if gr.citation_verdicts:
        supported = [v for v in gr.citation_verdicts if v.supported]
        unsupported = [v for v in gr.citation_verdicts if not v.supported]
        lines.append(f"### Per-citation grounding ({len(supported)}/{len(gr.citation_verdicts)} supported)")
        if unsupported:
            lines.append("")
            lines.append("**Unsupported citations** — the cited page does not contain evidence for the claim it's attached to:")
            for v in unsupported:
                reason = v.reason or "no reason given"
                lines.append(f"- {v.url} — {reason}")
        if supported:
            lines.append("")
            lines.append(f"{len(supported)} other citations passed grounding check.")
        lines.append("")

    # Quote matches (#4)
    if gr.quote_matches:
        ok = sum(1 for q in gr.quote_matches if q.get("matched"))
        lines.append(f"### Quote verification ({ok}/{len(gr.quote_matches)} quotes found verbatim on the cited page)")
        for q in gr.quote_matches:
            badge = "✓" if q.get("matched") else "✗"
            lines.append(f"- {badge} {q.get('url','')} — \"{q.get('quote','')}\"")
            if not q.get("matched") and q.get("reason"):
                lines.append(f"    - {q['reason']}")
        lines.append("")

    # Thin profile
    if gr.thin_profile:
        lines.append("### ⚠ Thin-profile warning")
        lines.append(
            f"The subject appears in only **{gr.thin_profile_mention_count}** fetched page(s). "
            "When public information is this sparse, models tend to fabricate plausible-sounding prose to "
            "fill the gap. Treat any assertion above that lacks a ✓ supported citation with skepticism."
        )
        lines.append("")

    # Disambiguation candidates (#8)
    if gr.disambiguation_candidates:
        lines.append(f"### Name collisions found in fetched content ({len(gr.disambiguation_candidates)})")
        lines.append("These name-like strings share tokens with the subject but aren't an exact match. "
                     "They may be distinct individuals or organisations that could be conflated.")
        for cand in gr.disambiguation_candidates:
            excerpt = cand.get("excerpt", "")
            lines.append(f"- **{cand.get('name', '')}** — {cand.get('source_url', '')}")
            if excerpt:
                lines.append(f"    - _…{excerpt}…_")
        lines.append("")

    if not (gr.ghost_urls or gr.dead_urls or gr.citation_verdicts or gr.thin_profile
            or gr.disambiguation_candidates or getattr(gr, "pipeline_corrupted", False)):
        lines.append("_No issues detected — every cited URL was fetched this run and all sampled citations were supported._")
        lines.append("")

    return "\n".join(lines)


def _read_checkpoint_from_stream(stream_file: Path) -> dict:
    """
    Parse an existing .stream file and reconstruct checkpoint state for resume.

    Returns a dict with:
      last_stage_completed  — highest stage number fully finished (0 = none)
      stage_outputs         — {str(stage_num): output_text} from stage_complete events
      fetched_urls          — list of URLs already fetched (to skip re-fetching)
      notes                 — list of all note content strings
      plan                  — latest plan content
      draft                 — latest draft content
    """
    checkpoint: dict = {
        "last_stage_completed": 0,
        "stage_outputs": {},
        "fetched_urls": [],
        "notes": [],
        "plan": "",
        "draft": "",
    }
    if not stream_file.exists():
        return checkpoint

    for raw in stream_file.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        t = ev.get("type", "")
        if t == "stage_complete":
            stage = int(ev.get("stage", 0))
            checkpoint["stage_outputs"][str(stage)] = ev.get("output", "")
            if stage > checkpoint["last_stage_completed"]:
                checkpoint["last_stage_completed"] = stage
        elif t == "fetch":
            url = ev.get("url", "")
            if url and url not in checkpoint["fetched_urls"]:
                checkpoint["fetched_urls"].append(url)
        elif t == "note_add":
            c = ev.get("content", "")
            if c:
                checkpoint["notes"].append(c)
        elif t == "plan_update":
            c = ev.get("content", "")
            if c:
                checkpoint["plan"] = c
        elif t == "draft_update":
            c = ev.get("content", "")
            if c:
                checkpoint["draft"] = c

    return checkpoint



def main() -> None:
    """
    Subprocess entry point for jobs launched via job_manager.launch_worker.

    On the dev2 branch, every job runs the **adaptive claims-model loop**.
    The pipeline helpers above (Agent / Crew construction, _instrumented_run,
    _stage_callback, etc.) are intentionally left in place but unused —
    they're preserved for diff readability against main and may be removed
    when the loop architecture is fully validated.

    The contract for the API/UI/MCP callers is unchanged:
      - read job_file["query"] (and optional depth)
      - emit stream events to {job_id}.stream
      - write result + status to {job_id}.json on completion
      - never raise — always close out the job file with a status
    """
    global _job_file

    if len(sys.argv) != 3:
        print("Usage: research_worker.py <job_id> <jobs_dir>", file=sys.stderr)
        sys.exit(1)

    job_id = sys.argv[1]
    jobs_dir = Path(sys.argv[2])
    job_file = jobs_dir / f"{job_id}.json"
    _job_file = job_file

    atexit.register(_mark_failed)
    signal.signal(signal.SIGTERM, _mark_failed)
    try:
        (jobs_dir / f"{job_id}.started").write_text("ok", encoding="utf-8")
    except Exception:
        pass

    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        query = data["query"]
        depth = str(data.get("depth", "medium")).lower()
    except Exception as exc:
        job_file.write_text(
            json.dumps({"status": "error", "query": "", "log": [], "result": f"Failed to read job file: {exc}"}),
            encoding="utf-8",
        )
        sys.exit(1)

    from scratchpad import Scratchpad
    from adaptive_worker import run_adaptive
    sp = Scratchpad(job_id, jobs_dir)
    sp.log(f"Starting adaptive research loop (depth={depth}) for: \"{query}\"")

    try:
        report, _claims = run_adaptive(
            query=query,
            depth=depth,
            job_id=job_id,
            jobs_dir=jobs_dir,
            log_fn=sp.log,
        )
        sp.stream_event({"type": "done", "status": "complete", "result": report})

        current = json.loads(job_file.read_text(encoding="utf-8"))
        current["status"] = "complete"
        current["result"] = report
        job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    except Exception as exc:
        try:
            current = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception:
            current = {"query": query, "log": []}
        if current.get("status") != "cancelled":
            current["status"] = "error"
            current["result"] = str(exc)
            sp.log(f"Adaptive loop failed: {exc}")
            sp.stream_event({"type": "done", "status": "error", "result": str(exc)})
            job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
