"""
scratchpad.py — Thread-safe progress logging for research jobs.

Tools and agents call log() as they work. The entry is appended to
the job's JSON file so get_research_result() can return a live
activity feed while the pipeline is still running.

For concurrent jobs, use the Scratchpad class directly so each job
has its own isolated instance with its own lock:

    sp = Scratchpad(job_id, jobs_dir)
    sp.log("Starting research pipeline")

The module-level log() function is kept for backward compatibility;
it reads job_id and jobs_dir from environment variables and is safe
only for single-job-per-process use.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()


class Scratchpad:
    """Per-job scratchpad for progress logging and stream event emission.

    Each research job should create its own instance so that concurrent
    jobs do not share state or file handles.
    """

    def __init__(self, job_id: str, jobs_dir: str | Path) -> None:
        self.job_id = job_id
        self.jobs_dir = Path(jobs_dir)
        self._lock = threading.Lock()

    def log(self, message: str, agent: str = "") -> None:
        """Append a progress entry to the job's .log file."""
        log_file = self.jobs_dir / f"{self.job_id}.log"
        ts = datetime.now().strftime("%H:%M:%S")
        line = json.dumps({"time": ts, "agent": agent, "message": message}) + "\n"
        with self._lock:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass
        # Mirror log entries to the stream file as well
        self.stream_event({"type": "log", "agent": agent, "message": message})

    def stream_event(self, event: dict) -> None:
        """Append a typed stream event to the job's .stream file.

        The SSE endpoint tails this file and forwards events to the browser
        in real time. Events must be JSON-serialisable dicts with a 'type' key.
        """
        stream_file = self.jobs_dir / f"{self.job_id}.stream"
        from datetime import datetime as _dt
        event = {**event, "ts": _dt.now().strftime("%H:%M:%S")}
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with open(stream_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass


def log(message: str, agent: str = "") -> None:
    """Append a progress entry to the job's log file (no-op if no job is active).

    Reads RESEARCH_JOB_ID and RESEARCH_JOBS_DIR from the environment.
    Safe for single-job-per-process use only. For concurrent jobs, use
    the Scratchpad class instead.
    """
    job_id = os.environ.get("RESEARCH_JOB_ID")
    jobs_dir = os.environ.get("RESEARCH_JOBS_DIR")
    if not job_id or not jobs_dir:
        return

    log_file = Path(jobs_dir) / f"{job_id}.log"
    ts = datetime.now().strftime("%H:%M:%S")
    line = json.dumps({"time": ts, "agent": agent, "message": message}) + "\n"

    with _lock:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def format_log(entries: list[dict], max_entries: int = 30) -> str:
    """Format the most recent log entries for display."""
    recent = entries[-max_entries:]
    lines = []
    for e in recent:
        prefix = f"[{e['time']}]"
        if e.get("agent"):
            prefix += f" [{e['agent']}]"
        lines.append(f"{prefix} {e['message']}")
    return "\n".join(lines)
