"""
job_manager.py — Shared job management utilities.

Used by both mcp_server.py and api_server.py to avoid duplicated logic
and ensure consistent behaviour across both interfaces.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import JOB_TIMEOUT_SECONDS as DEFAULT_JOB_TIMEOUT_SECONDS
# Orphaned file TTL: 7 days
ORPHAN_TTL_SECONDS = 7 * 24 * 60 * 60
# Grace period before a missing startup marker is treated as a crash
STARTUP_GRACE_SECONDS = 60


def create_job(
    query: str,
    jobs_dir: Path,
    clarifications: str = "",
    no_learn: bool = False,
    parent_report: str | None = None,
    gap_context: str | None = None,
    depth: str = "medium",
    thorough: bool = False,
) -> str:
    """Write a new job file and return the job_id."""
    job_id = str(uuid.uuid4())
    data: dict = {
        "status": "running",
        "query": query,
        "clarifications": clarifications,
        "no_learn": no_learn,
        "depth": depth,
        "thorough": thorough,
        "result": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_report:
        data["parent_report"] = parent_report
    if gap_context:
        data["gap_context"] = gap_context
    _write_job(job_id, jobs_dir, data)
    return job_id


def read_job(job_id: str, jobs_dir: Path) -> dict:
    """Read and return the job dict. Raises FileNotFoundError if missing."""
    return json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))


def _write_job(job_id: str, jobs_dir: Path, data: dict) -> None:
    """Write job data atomically via a temp-file rename."""
    job_file = jobs_dir / f"{job_id}.json"
    tmp = job_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(job_file)


def read_log(job_id: str, jobs_dir: Path, log_offset: int = 0) -> tuple[list[dict], int]:
    """
    Read log entries for a job from log_offset onward.
    Returns (new_entries_since_offset, total_entry_count).
    Malformed JSON lines are silently skipped.
    """
    log_file = jobs_dir / f"{job_id}.log"
    all_entries: list[dict] = []
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return all_entries[log_offset:], len(all_entries)


def save_report(query: str, result: str, reports_dir: Path) -> None:
    """Save a completed research report to disk. No-ops silently on failure."""
    try:
        if not result.strip():
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", query)[:60].strip("_")
        (reports_dir / f"{ts}_{slug}.md").write_text(
            f"# Query\n{query}\n\n{result}", encoding="utf-8"
        )
    except Exception:
        pass


def save_run(
    job_id: str,
    jobs_dir: Path,
    reports_dir: Path,
    query: str,
    result: str,
    status: str = "complete",
    started_at: str = "",
) -> str | None:
    """
    Save the research report *and* all run artifacts to reports_dir.

    Reads the job's .stream file before cleanup_job() deletes it, then
    writes to reports_dir/{prefix}/:
        meta.json       — run statistics and settings snapshot
        audit.jsonl     — every stream event (raw)
        plan.md         — final plan content (if any)
        notes.md        — all notes in order (if any)
        draft.md        — final working draft (if any)
        sources.json    — all URLs with title, category, confirmed flag

    The report itself is saved as reports_dir/{prefix}.md.
    Returns the prefix string on success, None on failure.
    Call this BEFORE cleanup_job() so the stream file is still present.
    """
    if not result.strip():
        return None

    try:
        # ── Shared filename prefix ─────────────────────────────────────────
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", query)[:60].strip("_")
        prefix = f"{ts}_{slug}"

        # ── Save report Markdown ───────────────────────────────────────────
        (reports_dir / f"{prefix}.md").write_text(
            f"# Query\n{query}\n\n{result}", encoding="utf-8"
        )

        # ── Parse stream events ────────────────────────────────────────────
        stream_file = jobs_dir / f"{job_id}.stream"
        events: list[dict] = []
        if stream_file.exists():
            for line in stream_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # ── Extract artifacts ──────────────────────────────────────────────
        plan          = ""
        notes:  list[str]       = []
        draft         = ""
        sources: dict[str, dict] = {}   # url → {title, category, confirmed}
        search_queries: list[str] = []
        fetch_urls:     list[str] = []
        step_count    = 0
        plan_updates  = 0
        draft_updates = 0
        agents_seen:  list[str] = []
        # New artifact collections
        thought_nodes: list[dict] = []
        threads:  list[dict] = []
        gap_notes: list[str] = []
        _current_thread: dict | None = None
        _current_stage: int = 1

        for ev in events:
            t = ev.get("type", "")
            if t == "plan_update":
                plan = ev.get("content", plan)
                plan_updates += 1
            elif t == "note_add":
                c = ev.get("content", "")
                if c:
                    notes.append(c)
                    if _current_thread is not None:
                        _current_thread["note_count"] = _current_thread.get("note_count", 0) + 1
                    if _current_stage == 3:
                        gap_notes.append(c)
            elif t == "draft_update":
                draft = ev.get("content", draft)
                draft_updates += 1
            elif t == "search":
                q = ev.get("query", "")
                if q:
                    search_queries.append(q)
                    _current_thread = {
                        "label":      q,
                        "queries":    [q],
                        "urls_fetched": [],
                        "note_count": 0,
                        "status":     "followed",
                        "stage":      _current_stage,
                    }
                    threads.append(_current_thread)
            elif t == "search_result":
                for r in ev.get("results", []):
                    url = r.get("url", "")
                    if url:
                        sources.setdefault(url, {
                            "title":    r.get("title", url),
                            "category": r.get("category", ""),
                            "confirmed": False,
                        })
            elif t == "fetch":
                url = ev.get("url", "")
                if url:
                    fetch_urls.append(url)
                    sources.setdefault(url, {
                        "title":    url,
                        "category": ev.get("category", ""),
                        "confirmed": False,
                    })
                    if _current_thread is not None:
                        _current_thread["urls_fetched"].append(url)
            elif t == "fetch_content":
                url = ev.get("url", "")
                if url and url in sources:
                    sources[url]["confirmed"] = True
            elif t == "step":
                step_count += 1
            elif t == "agent_switch":
                agent = ev.get("agent", "")
                _current_stage = ev.get("stage", _current_stage)
                if agent and agent not in agents_seen:
                    agents_seen.append(agent)
            elif t == "thought_node":
                label = ev.get("label", "")
                if label:
                    thought_nodes.append({
                        "id":       ev.get("id", ""),
                        "label":    label,
                        "rationale": ev.get("rationale", ""),
                        "ts":       ev.get("ts", ""),
                        "stage":    _current_stage,
                    })

        # ── Run metadata ───────────────────────────────────────────────────
        completed_at    = datetime.now(timezone.utc).isoformat()
        elapsed_seconds: int | None = None
        if started_at:
            try:
                s = datetime.fromisoformat(started_at)
                if s.tzinfo is None:
                    s = s.replace(tzinfo=timezone.utc)
                c = datetime.fromisoformat(completed_at)
                elapsed_seconds = int((c - s).total_seconds())
            except Exception:
                pass

        categories: dict[str, int] = {}
        for src in sources.values():
            cat = src.get("category") or "Unknown"
            categories[cat] = categories.get(cat, 0) + 1

        unique_queries = list(dict.fromkeys(search_queries))

        meta = {
            "prefix":             prefix,
            "query":              query,
            "status":             status,
            "started_at":         started_at,
            "completed_at":       completed_at,
            "elapsed_seconds":    elapsed_seconds,
            "model":              os.environ.get("LM_STUDIO_MODEL", ""),
            "base_url":           os.environ.get("LM_STUDIO_BASE_URL", ""),
            "search_count":       len(search_queries),
            "unique_query_count": len(unique_queries),
            "unique_queries":     unique_queries,
            "fetch_count":        len(fetch_urls),
            "unique_url_count":   len(sources),
            "source_categories":  categories,
            "plan_updates":       plan_updates,
            "note_count":         len(notes),
            "draft_updates":      draft_updates,
            "step_count":         step_count,
            "agents_seen":        agents_seen,
            "word_count":         len(result.split()),
            "report_file":        f"{prefix}.md",
            "thought_count":      len(thought_nodes),
            "thread_count":       len(threads),
            "gap_count":          len(gap_notes),
        }

        # ── Write artifacts directory ──────────────────────────────────────
        art_dir = reports_dir / prefix
        art_dir.mkdir(exist_ok=True)

        (art_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if events:
            with open(art_dir / "audit.jsonl", "w", encoding="utf-8") as fh:
                for ev in events:
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if plan:
            (art_dir / "plan.md").write_text(plan, encoding="utf-8")
        if notes:
            notes_md = "\n\n---\n\n".join(
                f"## Note {i + 1}\n{n}" for i, n in enumerate(notes)
            )
            (art_dir / "notes.md").write_text(notes_md, encoding="utf-8")
        if draft:
            (art_dir / "draft.md").write_text(draft, encoding="utf-8")
        if sources:
            src_list = [{"url": url, **info} for url, info in sources.items()]
            (art_dir / "sources.json").write_text(
                json.dumps(src_list, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if thought_nodes:
            (art_dir / "thought_tree.json").write_text(
                json.dumps(thought_nodes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if threads:
            (art_dir / "threads.json").write_text(
                json.dumps(threads, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if gap_notes:
            gap_md = "\n\n---\n\n".join(
                f"## Gap Note {i + 1}\n{n}" for i, n in enumerate(gap_notes)
            )
            (art_dir / "gaps.md").write_text(gap_md, encoding="utf-8")

        # Copy grounding-validator output (if the worker produced one) into
        # the artifacts directory so the UI/API can surface citation checks.
        grounding_src = jobs_dir / f"{job_id}.grounding.json"
        if grounding_src.exists():
            try:
                (art_dir / "grounding.json").write_text(
                    grounding_src.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass

        # Copy the fetched-page content cache so the grounding results can
        # be re-audited later without re-fetching every URL.
        fetched_src = jobs_dir / f"{job_id}.fetched.jsonl"
        if fetched_src.exists():
            try:
                (art_dir / "fetched.jsonl").write_text(
                    fetched_src.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass

        return prefix

    except Exception:
        # Fallback: at minimum save the report
        try:
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^a-zA-Z0-9_-]", "_", query)[:60].strip("_")
            prefix = f"{ts}_{slug}"
            (reports_dir / f"{prefix}.md").write_text(
                f"# Query\n{query}\n\n{result}", encoding="utf-8"
            )
            return prefix
        except Exception:
            return None


def cleanup_job(job_id: str, jobs_dir: Path) -> None:
    """Delete the job JSON, log, stream, grounding, fetched-cache, and startup-marker files."""
    for suffix in (".json", ".log", ".stream", ".started", ".tmp",
                   ".grounding.json", ".fetched.jsonl"):
        try:
            (jobs_dir / f"{job_id}{suffix}").unlink()
        except FileNotFoundError:
            pass


def launch_worker(job_id: str, jobs_dir: Path, worker_script: Path) -> None:
    """
    Launch research_worker.py as a fully detached subprocess that survives
    MCP server or API server restarts.
    """
    kwargs: dict = {
        "close_fds": True,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, str(worker_script), job_id, str(jobs_dir)],
        **kwargs,
    )
    try:
        data = read_job(job_id, jobs_dir)
        data["pid"] = proc.pid
        _write_job(job_id, jobs_dir, data)
    except Exception:
        pass


def resume_job(job_id: str, jobs_dir: Path) -> dict:
    """
    Reset a failed/errored job so it can be resumed from where it left off.

    - Strips the trailing 'done' event from the .stream file so that SSE replay
      does not fire the old error event and prematurely close the browser connection.
    - Resets status to 'running' and sets resume=True so the worker reads the checkpoint
    - Returns the updated job dict so callers can re-launch the worker
    Raises FileNotFoundError if the job doesn't exist.
    Raises ValueError if the job is not in a resumable state (must be 'error').
    """
    data = read_job(job_id, jobs_dir)
    status = data.get("status")
    if status != "error":
        raise ValueError(f"Job is not resumable (status={status!r}). Only 'error' jobs can be resumed.")

    # Strip any trailing 'done' event from the stream file.  The SSE endpoint
    # replays the file from line 0, so if the old error 'done' event is present
    # the browser will fire stopElapsed/showJobError and close the EventSource
    # before the resumed worker's new events can arrive.
    stream_file = jobs_dir / f"{job_id}.stream"
    if stream_file.exists():
        try:
            lines = stream_file.read_text(encoding="utf-8").splitlines()
            cleaned = [
                ln for ln in lines
                if '"type":"done"' not in ln.replace(" ", "") and
                   '"type": "done"' not in ln
            ]
            stream_file.write_text(
                "\n".join(cleaned) + ("\n" if cleaned else ""),
                encoding="utf-8",
            )
        except Exception:
            pass

    data["status"] = "running"
    data["result"] = ""
    data["resume"] = True
    data["resumed_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, jobs_dir, data)
    return data


def cancel_job(job_id: str, jobs_dir: Path) -> dict:
    """
    Cancel a running job.
    - Marks the job as 'cancelled' in the JSON file.
    - Sends SIGTERM to the worker process, then SIGKILL if it does not exit.
    Returns the updated job dict.
    Raises FileNotFoundError if job not found.
    Raises ValueError (with the current status string) if already in a terminal state.
    """
    data = read_job(job_id, jobs_dir)

    status = data.get("status")
    if status in ("complete", "cancelled", "error"):
        raise ValueError(status)

    pid = data.get("pid")

    # Mark cancelled before killing so the worker's signal handler sees it
    data["status"] = "cancelled"
    data["result"] = "Job was cancelled by user."
    _write_job(job_id, jobs_dir, data)

    if pid:
        if sys.platform == "win32":
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            else:
                time.sleep(0.5)
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    return data


def check_job_health(
    job_id: str,
    jobs_dir: Path,
    data: dict,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
) -> dict:
    """
    Inspect a running job and return updated data if a problem is found:
      - Timed out: job has been running longer than timeout_seconds.
      - Crashed at startup: no .started marker exists after the grace period.
    Returns data unchanged if the job appears healthy.
    """
    if data.get("status") != "running":
        return data

    started_at_str = data.get("started_at")
    if not started_at_str:
        return data

    try:
        started_at = datetime.fromisoformat(started_at_str)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started_at).total_seconds()
    except Exception:
        return data

    # Check for hard timeout
    if age > timeout_seconds:
        data = {**data, "status": "error",
                "result": f"Job timed out after {int(age / 60)} minutes."}
        _write_job(job_id, jobs_dir, data)
        return data

    # Check for startup crash: worker should write a .started marker quickly
    marker = jobs_dir / f"{job_id}.started"
    if age > STARTUP_GRACE_SECONDS and not marker.exists():
        data = {**data, "status": "error",
                "result": "Worker process failed to start (no startup marker found)."}
        _write_job(job_id, jobs_dir, data)
        return data

    return data


def cleanup_orphans(jobs_dir: Path, ttl_seconds: int = ORPHAN_TTL_SECONDS) -> None:
    """Delete any job/log files older than ttl_seconds."""
    cutoff = datetime.now(timezone.utc).timestamp() - ttl_seconds
    for f in jobs_dir.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def sweep_stale_jobs(jobs_dir: Path, timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS) -> int:
    """
    Scan all job files and mark stuck 'running' jobs as errors.
    Called at server startup so zombie jobs from crashed workers don't linger.
    Returns the number of jobs marked as stale.
    """
    count = 0
    for f in jobs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("status") != "running":
                continue
            job_id = f.stem
            updated = check_job_health(job_id, jobs_dir, data, timeout_seconds)
            if updated.get("status") != "running":
                count += 1
        except Exception:
            pass
    return count


def find_running_job(query: str, jobs_dir: Path) -> str | None:
    """
    Return the job_id of an already-running job with the same query, or None.
    Used to prevent duplicate jobs from MCP timeout retries.
    """
    query_norm = query.strip().lower()
    for f in jobs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("status") == "running" and data.get("query", "").strip().lower() == query_norm:
                return f.stem
        except Exception:
            pass
    return None
