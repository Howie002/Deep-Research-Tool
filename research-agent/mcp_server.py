"""
mcp_server.py — Exposes the research crew as an MCP tool.

Because LM Studio kills the MCP server process when it disconnects,
all research runs in a fully detached subprocess (research_worker.py)
that survives MCP restarts. Results are persisted to disk in the
jobs/ directory.

Workflow:
  1. start_research(query)        — creates a job file, launches a detached
                                    worker process, returns job_id immediately.
  2. get_research_result(job_id)  — reads the job file and returns the result
                                    (or "still running" if not done yet).

LM Studio / Claude Desktop config:
    {
      "mcpServers": {
        "research-agent": {
          "command": "C:\\...\\Deep Research Agent\\.venv\\Scripts\\python.exe",
          "args": ["C:\\...\\Deep Research Agent\\mcp_server.py"]
        }
      }
    }
"""
from pathlib import Path

from fastmcp import FastMCP

from config import MCP_SERVER_NAME
from job_manager import (
    check_job_health,
    cleanup_job,
    cleanup_orphans,
    create_job,
    find_running_job,
    launch_worker,
    read_job,
    read_log,
    save_report,
    sweep_stale_jobs,
)
from scratchpad import format_log

BASE_DIR      = Path(__file__).parent
JOBS_DIR      = BASE_DIR / "jobs"
REPORTS_DIR   = BASE_DIR / "reports"
WORKER_SCRIPT = BASE_DIR / "research_worker.py"

JOBS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

# Mark any zombie "running" jobs from a previous server session as stale on startup
sweep_stale_jobs(JOBS_DIR)

mcp = FastMCP(
    name=MCP_SERVER_NAME,
    instructions=(
        "This server runs a multi-agent research pipeline. "
        "IMPORTANT: on local hardware the pipeline takes 30-120 minutes — be patient. "
        "Step 1 — call `start_research` with your query. It returns a job_id immediately. "
        "Step 2 — call `get_research_result` with that job_id and log_offset=0. "
        "Each response includes an updated log_offset — pass it in your next call. "
        "Only call again when a stage update is returned or after 5 minutes. "
        "The result will be a full Markdown report when complete."
    ),
)


@mcp.tool()
def start_research(query: str) -> str:
    """
    Start a multi-agent research job and return a job_id immediately.

    The pipeline runs in a background process that survives disconnects:
      1. A Research Specialist searches the web from multiple angles.
      2. A Critical Analyst cross-checks and verifies key claims.
      3. A Report Synthesizer produces a cited Markdown report.

    After calling this, use get_research_result(job_id) to poll for the result.
    Poll every 5 minutes — on local hardware the pipeline takes 30-120 minutes.

    Args:
        query: The research question or topic to investigate.

    Returns:
        A job_id string to use with get_research_result.
    """
    cleanup_orphans(JOBS_DIR)
    query = query.strip()

    # Prevent duplicate jobs from MCP timeout retries
    existing = find_running_job(query, JOBS_DIR)
    if existing:
        return (
            f"A research job for this query is already running.\n"
            f"job_id: {existing}\n\n"
            f"Call get_research_result('{existing}') to check progress."
        )

    job_id = create_job(query, JOBS_DIR)
    try:
        launch_worker(job_id, JOBS_DIR, WORKER_SCRIPT)
    except Exception as exc:
        return f"Failed to start research worker: {exc}"

    return (
        f"Research job started.\n"
        f"job_id: {job_id}\n\n"
        f"Call get_research_result('{job_id}') in 5 minutes to check progress. "
        f"On local hardware the pipeline takes 30-120 minutes."
    )


@mcp.tool()
def get_research_result(job_id: str, log_offset: int = 0) -> str:
    """
    Check the status of a research job and retrieve the result when complete.

    To avoid re-reading log entries you've already seen, pass the log_offset
    returned by the previous call. Start with 0.

    Args:
        job_id: The job_id returned by start_research.
        log_offset: Number of log entries already seen (default 0). Pass the
                    value from the previous response to receive only new activity.

    Returns:
        If still running: any new activity since log_offset, plus the new offset
        to pass next time. Check back in 30-60 seconds.
        If complete: the full Markdown research report with citations.
        If error: the error message.
    """
    job_file = JOBS_DIR / f"{job_id}.json"

    if not job_file.exists():
        return f"No job found with id '{job_id}'. Please check the job_id and try again."

    try:
        data = read_job(job_id, JOBS_DIR)
    except Exception as exc:
        return f"Failed to read job file: {exc}"

    data = check_job_health(job_id, JOBS_DIR, data)
    status = data.get("status", "unknown")
    new_entries, new_offset = read_log(job_id, JOBS_DIR, log_offset)

    if status == "running":
        stage_entries = [e for e in new_entries if e.get("agent")]
        if stage_entries:
            return (
                f"Research in progress — stage update:\n\n"
                f"{format_log(stage_entries)}\n\n"
                f"log_offset for next call: {new_offset}\n"
                f"Check back in 5 minutes."
            )
        return f"Research in progress. log_offset for next call: {new_offset}. Check back in 5 minutes."

    if status == "error":
        return f"Research job failed with error:\n\n{data.get('result', 'Unknown error')}"

    # Complete — save report, clean up, return result
    result = data.get("result", "")
    save_report(data.get("query", ""), result, REPORTS_DIR)
    cleanup_job(job_id, JOBS_DIR)
    return result


if __name__ == "__main__":
    mcp.run()
