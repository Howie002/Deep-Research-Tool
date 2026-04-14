"""
api_server.py — FastAPI web server with job-based async research pipeline.

Uses the same detached-worker architecture as mcp_server.py so research
jobs survive server restarts. Also serves the web UI from static/.

Endpoints:
    POST /api/jobs                                    — start a job, returns job_id
    GET  /api/jobs/{job_id}?log_offset=0              — poll status and live log
    GET  /api/jobs/{job_id}/stream                    — SSE stream of live log entries
    POST /api/jobs/{job_id}/cancel                    — cancel a running job
    GET  /api/reports?tags=ai,finance                   — list saved reports (optional tag filter)
    GET  /api/reports/{filename}                        — retrieve a saved report
    POST /api/reports/{filename}/tags                   — replace tags on a report
    GET  /api/reports/{filename}/tags                   — get tags for a report
    GET  /api/reports/{filename}/export?format=pdf|docx — download report as PDF or DOCX
    GET  /                                            — web UI (static/index.html)
"""
from __future__ import annotations

import asyncio
import json as _json
import math
import os
import re
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import API_HOST, API_KEY, API_PORT, CORS_ORIGINS, MCP_SERVER_NAME
from job_manager import (
    cancel_job as _cancel_job,
    check_job_health,
    cleanup_job,
    cleanup_orphans,
    create_job as _create_job,
    find_running_job,
    launch_worker,
    read_job,
    read_log,
    save_report,
    save_run,
    sweep_stale_jobs,
)

BASE_DIR      = Path(__file__).parent
JOBS_DIR      = BASE_DIR / "jobs"
REPORTS_DIR   = BASE_DIR / "reports"
STATIC_DIR    = BASE_DIR / "static"
WORKER_SCRIPT = BASE_DIR / "research_worker.py"

JOBS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# Mark zombie "running" jobs from previous sessions as stale on startup
sweep_stale_jobs(JOBS_DIR)

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Research Agent",
    description="Multi-agent web research powered by CrewAI and LM Studio.",
    version="2.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ───────────────────────────────────────────────────────────────


@app.on_event("startup")
async def _startup() -> None:
    rebuild_index(REPORTS_DIR)


# ── Auth ───────────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: Optional[str] = Security(_api_key_header)) -> None:
    """Validate X-API-Key header when API_KEY is configured."""
    if not API_KEY:
        return
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key.")


# ── Schemas ────────────────────────────────────────────────────────────────


class JobCreateRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000, description="The research question or topic.")
    clarifications: str = Field(default="", max_length=2000, description="Optional pre-run answers to scope the research.")


class JobCreateResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    status: str               # running | complete | error
    log: list[dict]           # new log entries since log_offset
    new_offset: int
    result: Optional[str] = None


class CancelJobResponse(BaseModel):
    job_id: str
    status: str


class ReportListItem(BaseModel):
    filename: str
    query: str
    created: str
    tags: list[str] = []


class ReportListResponse(BaseModel):
    reports: list[ReportListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReportDetailResponse(BaseModel):
    filename: str
    content: str


class TagUpdateRequest(BaseModel):
    tags: list[str] = Field(..., description="Replacement tag list. Max 10 tags, each max 32 chars, lowercase alphanumeric + hyphens.")


class TagsResponse(BaseModel):
    filename: str
    tags: list[str]


class ReportSearchItem(BaseModel):
    filename: str
    title: str
    created_at: str
    snippet: str
    word_count: int


class ReportSearchResponse(BaseModel):
    results: list[ReportSearchItem]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── Report index ───────────────────────────────────────────────────────────


def _extract_title(content: str, filename: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return Path(filename).stem


def _build_index_entry(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    return {
        "filename": path.name,
        "title": _extract_title(content, path.name),
        "created_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "snippet": content[:300],
        "word_count": len(content.split()),
    }


def _load_index(reports_dir: Path) -> list[dict]:
    idx_file = reports_dir / "reports_index.json"
    if idx_file.exists():
        try:
            return _json.loads(idx_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_index(reports_dir: Path, entries: list[dict]) -> None:
    idx_file = reports_dir / "reports_index.json"
    tmp = idx_file.with_suffix(".tmp")
    tmp.write_text(_json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(idx_file)


def rebuild_index(reports_dir: Path) -> list[dict]:
    """Rebuild the full index from all .md files. Returns the new entries list."""
    entries = []
    for f in sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            entries.append(_build_index_entry(f))
        except Exception:
            continue
    _save_index(reports_dir, entries)
    return entries


def _search_index(entries: list[dict], query: str) -> list[dict]:
    """Return entries matching query, title hits before snippet-only hits, newest first."""
    q = query.lower()
    title_hits: list[dict] = []
    snippet_hits: list[dict] = []
    for e in entries:
        if q in e.get("title", "").lower():
            title_hits.append(e)
        elif q in e.get("snippet", "").lower():
            snippet_hits.append(e)
    title_hits.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    snippet_hits.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return title_hits + snippet_hits


# ── Report tags storage ────────────────────────────────────────────────────

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TAGS_LOCK = threading.Lock()


def _validate_tags(tags: list[str]) -> str | None:
    """Return an error message string if tags are invalid, else None."""
    if len(tags) > 10:
        return "Too many tags: maximum 10 allowed."
    for tag in tags:
        if not tag:
            return "Tags must not be empty strings."
        if len(tag) > 32:
            return f"Tag '{tag}' exceeds 32 characters."
        if not _TAG_RE.match(tag):
            return f"Tag '{tag}' is invalid: use lowercase letters, digits, and hyphens only."
    return None


def _load_tags(reports_dir: Path) -> dict[str, list[str]]:
    tags_file = reports_dir / "reports_tags.json"
    if tags_file.exists():
        try:
            return _json.loads(tags_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_tags(reports_dir: Path, data: dict[str, list[str]]) -> None:
    tags_file = reports_dir / "reports_tags.json"
    tmp = tags_file.with_suffix(".tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(tags_file)


# ── Helpers ────────────────────────────────────────────────────────────────


# ── API endpoints ──────────────────────────────────────────────────────────


@app.post("/api/jobs", response_model=JobCreateResponse, dependencies=[Depends(verify_api_key)])
@limiter.limit("5/minute")
async def start_job(request: Request, body: JobCreateRequest) -> JobCreateResponse:
    """Start a research job. Returns a job_id to poll with GET /api/jobs/{job_id}."""
    cleanup_orphans(JOBS_DIR)
    query = body.query.strip()

    # Return existing job_id if the same query is already running
    existing = find_running_job(query, JOBS_DIR)
    if existing:
        return JobCreateResponse(job_id=existing)

    job_id = _create_job(query, JOBS_DIR, clarifications=body.clarifications)
    try:
        launch_worker(job_id, JOBS_DIR, WORKER_SCRIPT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start research worker: {exc}")
    return JobCreateResponse(job_id=job_id)


@app.post("/api/jobs/{job_id}/cancel", response_model=CancelJobResponse, dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str) -> CancelJobResponse:
    """Cancel a running job. 404 if not found, 409 if already in a terminal state."""
    job_file = JOBS_DIR / f"{job_id}.json"
    if not job_file.exists():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    try:
        _cancel_job(job_id, JOBS_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"Job is already in terminal state: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to cancel job: {exc}")
    return CancelJobResponse(job_id=job_id, status="cancelled")


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str, log_offset: int = 0) -> JobStatusResponse:
    """Poll a running job. Pass log_offset from the previous response to get only new entries."""
    job_file = JOBS_DIR / f"{job_id}.json"
    if not job_file.exists():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    try:
        data = read_job(job_id, JOBS_DIR)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read job file: {exc}")

    data = check_job_health(job_id, JOBS_DIR, data)
    status = data.get("status", "unknown")
    new_log, new_offset = read_log(job_id, JOBS_DIR, log_offset)

    if status == "running":
        return JobStatusResponse(status="running", log=new_log, new_offset=new_offset)

    if status == "error":
        return JobStatusResponse(
            status="error",
            log=new_log,
            new_offset=new_offset,
            result=data.get("result", "Unknown error"),
        )

    # Complete — save report + artifacts, update index, clean up, return result
    result = data.get("result", "")
    save_run(job_id, JOBS_DIR, REPORTS_DIR,
             data.get("query", ""), result,
             status="complete",
             started_at=data.get("started_at", ""))
    rebuild_index(REPORTS_DIR)
    cleanup_job(job_id, JOBS_DIR)
    return JobStatusResponse(status="complete", log=new_log, new_offset=new_offset, result=result)


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """SSE stream of rich typed events for a running job.

    Tails the job's .stream file (written by the worker) and forwards each
    JSONL event to the browser as an SSE ``data:`` line.  Events have a
    ``type`` field: search | search_result | fetch | fetch_content | step |
    agent_switch | plan_update | note_add | draft_update | log | done.

    The stream closes when a ``{"type":"done"}`` event is received or when
    the job file disappears.  A keepalive comment is sent every 15 s.
    """
    job_file   = JOBS_DIR / f"{job_id}.json"
    stream_file = JOBS_DIR / f"{job_id}.stream"

    if not job_file.exists():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    async def event_generator() -> AsyncGenerator[str, None]:
        line_pos = 0
        last_keepalive = asyncio.get_event_loop().time()
        done = False

        # Wait up to 30 s for the stream file to appear (worker may take a moment to start)
        for _ in range(30):
            if stream_file.exists():
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

        while not done:
            now = asyncio.get_event_loop().time()
            if now - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = now

            # Forward any new lines from the stream file
            if stream_file.exists():
                try:
                    lines = stream_file.read_text(encoding="utf-8").splitlines()
                    for line in lines[line_pos:]:
                        line = line.strip()
                        if not line:
                            continue
                        yield f"data: {line}\n\n"
                        try:
                            ev = _json.loads(line)
                            if ev.get("type") == "done":
                                status = ev.get("status", "complete")
                                result = ev.get("result", "")
                                if status == "complete":
                                    try:
                                        jdata = read_job(job_id, JOBS_DIR)
                                        save_run(job_id, JOBS_DIR, REPORTS_DIR,
                                                 jdata.get("query", ""), result,
                                                 status="complete",
                                                 started_at=jdata.get("started_at", ""))
                                        rebuild_index(REPORTS_DIR)
                                        cleanup_job(job_id, JOBS_DIR)
                                    except Exception:
                                        pass
                                done = True
                        except Exception:
                            pass
                    line_pos = len(lines)
                except Exception:
                    pass

            if not done:
                # Fallback: if job file is gone or terminal, close the stream
                if not job_file.exists():
                    yield f"data: {_json.dumps({'type': 'done', 'status': 'complete'})}\n\n"
                    done = True
                    continue
                try:
                    jdata = read_job(job_id, JOBS_DIR)
                    jdata = check_job_health(job_id, JOBS_DIR, jdata)
                    if jdata.get("status") not in ("running",):
                        status = jdata.get("status", "error")
                        result = jdata.get("result", "")
                        if status == "complete":
                            save_run(job_id, JOBS_DIR, REPORTS_DIR,
                                     jdata.get("query", ""), result,
                                     status="complete",
                                     started_at=jdata.get("started_at", ""))
                            rebuild_index(REPORTS_DIR)
                            cleanup_job(job_id, JOBS_DIR)
                        yield f"data: {_json.dumps({'type': 'done', 'status': status, 'result': result})}\n\n"
                        done = True
                        continue
                except FileNotFoundError:
                    yield f"data: {_json.dumps({'type': 'done', 'status': 'complete'})}\n\n"
                    done = True
                    continue
                except Exception:
                    pass

                await asyncio.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/reports", response_model=ReportListResponse)
async def list_reports(page: int = 1, page_size: int = 20, tags: Optional[str] = None) -> ReportListResponse:
    """List all saved research reports, newest first, with pagination.

    Optional ``tags`` query parameter accepts a comma-separated list (e.g. ``tags=ai,finance``);
    only reports matching ALL specified tags are returned.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    filter_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    with _TAGS_LOCK:
        all_tags = _load_tags(REPORTS_DIR)

    items = []
    for f in sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            report_tags = all_tags.get(f.name, [])
            if filter_tags and not all(t in report_tags for t in filter_tags):
                continue
            lines = f.read_text(encoding="utf-8").splitlines()
            query = lines[1] if len(lines) > 1 else f.stem
            created = datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d, %Y %H:%M")
            items.append(ReportListItem(filename=f.name, query=query, created=created, tags=report_tags))
        except Exception:
            continue
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size))
    start = (page - 1) * page_size
    return ReportListResponse(
        reports=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@app.get("/api/reports/search", response_model=ReportSearchResponse)
async def search_reports(q: str = "", page: int = 1, page_size: int = 20) -> ReportSearchResponse:
    """Full-text search over report titles and snippets.

    Returns results ranked by relevance (title matches before snippet-only matches),
    with ties broken by ``created_at`` descending. Returns an empty list when no
    reports match — never 404.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    entries = _load_index(REPORTS_DIR)
    matched = _search_index(entries, q.strip()) if q.strip() else sorted(
        entries, key=lambda e: e.get("created_at", ""), reverse=True
    )
    total = len(matched)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    start = (page - 1) * page_size
    page_entries = matched[start : start + page_size]
    return ReportSearchResponse(
        results=[ReportSearchItem(**e) for e in page_entries],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@app.post("/api/reports/reindex", response_model=ReportSearchResponse)
async def reindex_reports() -> ReportSearchResponse:
    """Force a full rebuild of the report index from disk.

    Returns the updated index as a search response (all reports, page 1).
    """
    entries = rebuild_index(REPORTS_DIR)
    total = len(entries)
    return ReportSearchResponse(
        results=[ReportSearchItem(**e) for e in entries[:20]],
        total=total,
        page=1,
        page_size=20,
        total_pages=max(1, math.ceil(total / 20)) if total else 1,
    )


@app.get("/api/reports/{filename}", response_model=ReportDetailResponse)
async def get_report(filename: str) -> ReportDetailResponse:
    """Retrieve a saved report by filename."""
    filename = Path(filename).name  # prevent directory traversal
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    report_path = REPORTS_DIR / filename
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Report '{filename}' not found.")
    return ReportDetailResponse(
        filename=filename,
        content=report_path.read_text(encoding="utf-8"),
    )


@app.post("/api/reports/{filename}/tags", response_model=TagsResponse)
async def set_tags(filename: str, body: TagUpdateRequest) -> TagsResponse:
    """Replace all tags on a report. Body: {"tags": ["ai", "finance"]}."""
    filename = Path(filename).name  # prevent directory traversal
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    if not (REPORTS_DIR / filename).exists():
        raise HTTPException(status_code=404, detail=f"Report '{filename}' not found.")
    err = _validate_tags(body.tags)
    if err:
        raise HTTPException(status_code=400, detail=err)
    with _TAGS_LOCK:
        data = _load_tags(REPORTS_DIR)
        data[filename] = body.tags
        _save_tags(REPORTS_DIR, data)
    return TagsResponse(filename=filename, tags=body.tags)


@app.get("/api/reports/{filename}/tags", response_model=TagsResponse)
async def get_tags(filename: str) -> TagsResponse:
    """Return current tags for a report."""
    filename = Path(filename).name  # prevent directory traversal
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    if not (REPORTS_DIR / filename).exists():
        raise HTTPException(status_code=404, detail=f"Report '{filename}' not found.")
    with _TAGS_LOCK:
        data = _load_tags(REPORTS_DIR)
    return TagsResponse(filename=filename, tags=data.get(filename, []))


_PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400&display=swap');
* { box-sizing: border-box; }
body {
    font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.7;
    color: #1a1a2e;
    margin: 0;
    padding: 0;
}
.section {
    padding: 0 60pt;
    margin-bottom: 32pt;
}
.section-break {
    break-before: page;
}
.section-title {
    font-size: 8pt;
    letter-spacing: .15em;
    text-transform: uppercase;
    color: #6366f1;
    font-weight: 600;
    margin-bottom: 12pt;
    padding-bottom: 6pt;
    border-bottom: 1px solid #e0e0f0;
}
h1 { font-size: 18pt; font-weight: 700; color: #0f0f1e; margin: 20pt 0 8pt; }
h2 { font-size: 13pt; font-weight: 700; color: #1a1a3e; margin: 16pt 0 6pt; border-bottom: 1px solid #e8e8f4; padding-bottom: 4pt; }
h3 { font-size: 11pt; font-weight: 600; color: #2a2a4e; margin: 12pt 0 4pt; }
p  { margin: 0 0 8pt; }
ul, ol { padding-left: 18pt; margin: 0 0 8pt; }
li { margin-bottom: 3pt; }
code {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 9pt;
    background: #f0f0f8;
    padding: 1pt 4pt;
    border-radius: 3pt;
}
pre {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 9pt;
    background: #f0f0f8;
    padding: 10pt;
    border-radius: 4pt;
    overflow-wrap: break-word;
    white-space: pre-wrap;
    margin: 0 0 8pt;
}
a { color: #6366f1; text-decoration: none; }
blockquote { border-left: 3pt solid #6366f1; margin: 0 0 8pt 0; padding: 4pt 0 4pt 12pt; color: #4a4a6a; }
table { width: 100%; border-collapse: collapse; margin: 0 0 10pt; font-size: 9.5pt; }
th { background: #f0f0fa; font-weight: 600; padding: 6pt 8pt; border: 1pt solid #d0d0e8; text-align: left; }
td { padding: 5pt 8pt; border: 1pt solid #e0e0f0; vertical-align: top; }
.note-card {
    background: #f8f8ff;
    border: 1pt solid #d8d8f0;
    border-left: 3pt solid #8b5cf6;
    border-radius: 4pt;
    padding: 8pt 10pt;
    margin-bottom: 8pt;
    font-size: 10pt;
}
.source-row {
    display: flex;
    gap: 8pt;
    padding: 6pt 0;
    border-bottom: 1pt solid #ebebf8;
    font-size: 9.5pt;
}
.badge {
    font-size: 7.5pt;
    font-weight: 600;
    padding: 1.5pt 5pt;
    border-radius: 10pt;
    white-space: nowrap;
}
.badge-confirmed { background: #dcfce7; color: #166534; }
.badge-found     { background: #ede9fe; color: #5b21b6; }
/* ── Cover page ── */
.cover {
    min-height: 297mm;
    display: flex;
    flex-direction: column;
    break-after: page;
    background: #fff;
}
.cover-bar { background: #6366f1; height: 6pt; }
.cover-body {
    flex: 1;
    padding: 52pt 60pt 32pt;
    display: flex;
    flex-direction: column;
    justify-content: center;
}
.cover-eyebrow {
    font-size: 7.5pt;
    letter-spacing: .2em;
    text-transform: uppercase;
    color: #6366f1;
    font-weight: 700;
    margin-bottom: 22pt;
}
.cover-title {
    font-size: 26pt;
    font-weight: 700;
    color: #0f0f1e;
    line-height: 1.22;
    margin: 0 0 18pt;
    max-width: 78%;
}
.cover-abstract {
    font-size: 11pt;
    color: #4a4a6a;
    line-height: 1.65;
    max-width: 72%;
    margin: 0 0 26pt;
    border-left: 3pt solid #6366f1;
    padding-left: 14pt;
    font-style: italic;
}
.cover-meta {
    font-size: 8.5pt;
    color: #9090b0;
    display: flex;
    gap: 10pt;
}
.cover-meta-sep { color: #d0d0e8; }
.cover-stats {
    display: flex;
    border-top: 1pt solid #e8e8f4;
    padding: 16pt 60pt 18pt;
    background: #f8f8ff;
}
.cover-stat { flex: 1; text-align: center; }
.stat-n {
    display: block;
    font-size: 19pt;
    font-weight: 700;
    color: #6366f1;
    line-height: 1.1;
}
.stat-l {
    display: block;
    font-size: 7pt;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: #9090b0;
    font-weight: 600;
    margin-top: 3pt;
}
sup.cite-ref {
    color: #6366f1;
    font-size: 7pt;
    font-weight: 700;
    vertical-align: super;
}
.cite-cat {
    margin-left: 4pt;
    font-size: 7pt;
    background: #ede9fe;
    color: #5b21b6;
    padding: 0.5pt 4pt;
    border-radius: 8pt;
    font-weight: 600;
}
@page { margin: 0; size: A4; }
"""


_CITE_RE = re.compile(r'【(https?://[^|】\s]+)\s*\|([^】]*)】')

_ALL_SECTIONS: set[str] = {"plan", "notes", "sources", "stats", "tree"}


def _generate_cover_title(query: str, report_md: str) -> tuple[str, str]:
    """
    Return (title, abstract).
    Title:    first plausible heading from the report body; falls back to
              title-cased query (max 14 words).
    Abstract: first substantive sentence from the ## Summary section.
    """
    title    = ""
    abstract = ""
    lines    = report_md.splitlines()

    _skip = {"query", "summary", "key findings", "sources", "references",
             "detailed analysis", "caveats", "caveats & uncertainties"}

    # Find first heading that looks like a real title
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            candidate = s.lstrip("#").strip()
            if candidate.lower() not in _skip and len(candidate) > 8:
                title = candidate
                break

    # Extract one-sentence abstract from ## Summary
    in_summary = False
    for line in lines:
        s = line.strip()
        if re.match(r"^#{1,3}\s+Summary", s, re.IGNORECASE):
            in_summary = True
            continue
        if in_summary:
            if s.startswith("#"):
                break
            if s and len(s) > 40:
                # Take up to the first sentence break
                first_sentence = re.split(r"(?<=[.!?])\s+", s)[0]
                abstract = first_sentence[:300] + ("…" if len(first_sentence) > 300 else "")
                break

    # Fall back: title-case the query, max 14 words
    if not title:
        words = query.split()
        if len(words) > 14:
            title = " ".join(words[:14]).title() + "…"
        else:
            title = query.title()

    return title, abstract


def _process_citations_pdf(
    text: str, sources_map: dict | None = None
) -> tuple[str, list[dict]]:
    """
    Scan *text* for 【url | category】 markers emitted by the LLM.
    Replace each with an inline (N) superscript and return
    (processed_text, refs) where refs is an ordered list of unique sources.
    """
    refs: list[dict] = []
    url_to_n: dict[str, int] = {}

    def _replace(m: re.Match) -> str:
        url = m.group(1).strip().rstrip("/")
        cat = m.group(2).strip()
        norm = url.rstrip("/")
        if norm not in url_to_n:
            n = len(refs) + 1
            # Try to get a human-readable title from sources_map
            title = ""
            if sources_map:
                info = sources_map.get(url) or sources_map.get(norm)
                if info:
                    title = info.get("title", "")
            if not title or title == url:
                try:
                    from urllib.parse import urlparse as _up
                    title = _up(url).hostname or url
                except Exception:
                    title = url[:60]
            refs.append({"n": n, "url": url, "title": title, "category": cat})
            url_to_n[norm] = n
        return f'<sup class="cite-ref">[{url_to_n[norm]}]</sup>'

    processed = _CITE_RE.sub(_replace, text)
    return processed, refs


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _build_tree_svg(tree: dict) -> str:
    """
    Render the research tree as an inline SVG for PDF embedding.
    Uses a vertical indented layout — one row per node, depth = x-indent.
    """
    _COLORS = {
        "query":  "#6366f1",
        "search": "#38bdf8",
        "url":    "#34d399",
        "fetch":  "#fbbf24",
        "note":   "#c084fc",
    }
    ROW_H    = 19
    INDENT   = 22
    LEFT_PAD = 10
    CR       = 4       # circle radius
    FONT_PT  = 8       # font size
    SVG_W    = 800
    MAX_ROWS = 160

    # DFS: collect (depth, type, label, url, confirmed)
    rows: list[tuple] = []

    def _walk(node: dict, depth: int) -> None:
        if len(rows) >= MAX_ROWS:
            return
        rows.append((
            depth,
            node.get("type", ""),
            node.get("label", "")[:80],
            node.get("url", ""),
            node.get("confirmed", False),
        ))
        for child in node.get("children", []):
            _walk(child, depth + 1)

    _walk(tree, 0)

    h = max(120, len(rows) * ROW_H + 30)
    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {SVG_W} {h}" width="{SVG_W}" height="{h}" '
        f'font-family="Inter,Helvetica,Arial,sans-serif">',
        f'<rect width="{SVG_W}" height="{h}" fill="#f8f8ff" rx="6"/>',
    ]

    # Track last y seen at each depth for drawing connector lines
    depth_last_cy: dict[int, float] = {}

    for i, (depth, ntype, label, url, confirmed) in enumerate(rows):
        cy = 16 + i * ROW_H + CR       # circle centre y
        cx = LEFT_PAD + depth * INDENT + CR  # circle centre x

        color = _COLORS.get(ntype, "#6a6a90")

        # Connector from parent
        if depth > 0 and (depth - 1) in depth_last_cy:
            parent_cy = depth_last_cy[depth - 1]
            px = LEFT_PAD + (depth - 1) * INDENT + CR
            lines.append(
                f'<line x1="{px}" y1="{parent_cy + CR}" '
                f'x2="{px}" y2="{cy}" '
                f'stroke="#d0d0e8" stroke-width="1.2"/>'
            )
            lines.append(
                f'<line x1="{px}" y1="{cy}" '
                f'x2="{cx - CR - 1}" y2="{cy}" '
                f'stroke="#d0d0e8" stroke-width="1.2"/>'
            )

        # Update depth tracker (clear deeper entries)
        depth_last_cy[depth] = cy
        for d in list(depth_last_cy):
            if d > depth:
                del depth_last_cy[d]

        # Circle
        lines.append(
            f'<circle cx="{cx}" cy="{cy}" r="{CR}" '
            f'fill="{color}" opacity="0.9"/>'
        )

        # Label text
        text_x = cx + CR + 5
        text_y = cy + int(FONT_PT * 0.38)
        lines.append(
            f'<text x="{text_x}" y="{text_y}" '
            f'font-size="{FONT_PT}pt" fill="#1a1a2e">'
            f'{_xml_escape(label)}</text>'
        )

        # Read/Found badge for URL nodes
        if ntype == "url":
            bx = SVG_W - 48
            if confirmed:
                lines.append(f'<rect x="{bx}" y="{cy - CR - 1}" width="38" height="11" rx="5" fill="#dcfce7"/>')
                lines.append(f'<text x="{bx + 5}" y="{cy + CR - 1}" font-size="6.5pt" font-weight="600" fill="#166534">Read</text>')
            else:
                lines.append(f'<rect x="{bx}" y="{cy - CR - 1}" width="38" height="11" rx="5" fill="#ede9fe"/>')
                lines.append(f'<text x="{bx + 5}" y="{cy + CR - 1}" font-size="6.5pt" font-weight="600" fill="#5b21b6">Found</text>')

    # Legend
    legend_y = h - 14
    legend_items = [("Query", "#6366f1"), ("Search", "#38bdf8"),
                    ("URL", "#34d399"), ("Fetch", "#fbbf24"), ("Note", "#c084fc")]
    lx = LEFT_PAD
    for leg_label, leg_color in legend_items:
        lines.append(f'<circle cx="{lx + 4}" cy="{legend_y}" r="3.5" fill="{leg_color}"/>')
        lines.append(
            f'<text x="{lx + 11}" y="{legend_y + 4}" '
            f'font-size="6.5pt" fill="#6a6a90">{leg_label}</text>'
        )
        lx += 56

    lines.append('</svg>')
    return "\n".join(lines)


def _build_export_html(
    report_md: str,
    art_dir: Path | None,
    query: str = "",
    sections: set[str] | None = None,
) -> str:
    """Build a complete styled HTML document for PDF export.

    ``sections`` controls which artifact sections are included.
    Pass ``None`` (or omit) to include all sections.
    Recognised keys: ``plan``, ``notes``, ``sources``, ``stats``, ``tree``.
    The Research Report is always included regardless.
    """
    if sections is None:
        sections = _ALL_SECTIONS

    import markdown as _md
    md = _md.Markdown(extensions=["tables", "fenced_code", "nl2br"])

    from datetime import datetime as _dt
    generated = _dt.now().strftime("%B %d, %Y at %H:%M")
    gen_short  = _dt.now().strftime("%B %d, %Y")

    # ── Load meta + sources early (used by cover AND body sections) ───────────
    meta: dict = {}
    sources_map: dict[str, dict] = {}
    if art_dir and art_dir.exists():
        meta_f = art_dir / "meta.json"
        if meta_f.exists():
            try:
                meta = _json.loads(meta_f.read_text(encoding="utf-8"))
            except Exception:
                pass
        src_f = art_dir / "sources.json"
        if src_f.exists():
            try:
                for s in _json.loads(src_f.read_text(encoding="utf-8")):
                    u = s.get("url", "").rstrip("/")
                    if u:
                        sources_map[u] = s
            except Exception:
                pass

    # ── Process citations (【url | category】 → [N] superscripts) ─────────────
    processed_report_md, refs = _process_citations_pdf(report_md, sources_map)

    # ── Cover page ─────────────────────────────────────────────────────────────
    cover_title, cover_abstract = _generate_cover_title(query or "Research Report", report_md)

    model_label = meta.get("model", "") or query or "Research Report"
    # Truncate model string to avoid overflow
    if len(model_label) > 40:
        model_label = model_label[:38] + "…"

    # Stats for cover snapshot bar
    elapsed = meta.get("elapsed_seconds")
    elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed else "—"
    snap_stats = [
        (str(meta.get("unique_url_count", "—")), "Sources"),
        (str(meta.get("fetch_count", "—")),      "Pages Read"),
        (str(meta.get("search_count", "—")),      "Searches"),
        (elapsed_str,                             "Run Time"),
    ]
    snap_html = "".join(
        f'<div class="cover-stat">'
        f'<span class="stat-n">{_xml_escape(n)}</span>'
        f'<span class="stat-l">{_xml_escape(l)}</span>'
        f'</div>'
        for n, l in snap_stats
    )

    abstract_html = (
        f'<p class="cover-abstract">{_xml_escape(cover_abstract)}</p>'
        if cover_abstract else ""
    )

    html_parts = [f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"/>
<style>{_PDF_CSS}</style>
</head><body>
<div class="cover">
  <div class="cover-bar"></div>
  <div class="cover-body">
    <div class="cover-eyebrow">Research Agent — Intelligence Report</div>
    <h1 class="cover-title">{_xml_escape(cover_title)}</h1>
    {abstract_html}
    <div class="cover-meta">
      <span>{gen_short}</span>
      <span class="cover-meta-sep">·</span>
      <span>{_xml_escape(model_label)}</span>
    </div>
  </div>
  <div class="cover-stats">{snap_html}</div>
</div>
"""]

    # Build a url→citation-number lookup for use in the Sources section
    url_to_n: dict[str, int] = {r["url"].rstrip("/"): r["n"] for r in refs}

    # ── Main report ───────────────────────────────────────────────────────────
    html_parts.append('<div class="section">')
    html_parts.append('<div class="section-title">Research Report</div>')
    md.reset()
    html_parts.append(md.convert(processed_report_md))
    # References note — directs reader to the Sources section below
    if refs:
        html_parts.append(
            '<p style="font-size:8.5pt;color:#6a6a90;margin-top:12pt;font-style:italic">'
            'Numbered citations [N] refer to entries in the Sources section.</p>'
        )
    html_parts.append('</div>')

    if art_dir is None or not art_dir.exists():
        html_parts.append('</body></html>')
        return "".join(html_parts)

    # ── Research Plan ─────────────────────────────────────────────────────────
    plan_file = art_dir / "plan.md"
    if "plan" in sections and plan_file.exists():
        plan_text = plan_file.read_text(encoding="utf-8").strip()
        if plan_text:
            html_parts.append('<div class="section section-break">')
            html_parts.append('<div class="section-title">Research Plan</div>')
            md.reset()
            html_parts.append(md.convert(plan_text))
            html_parts.append('</div>')

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes_file = art_dir / "notes.md"
    if "notes" in sections and notes_file.exists():
        notes_text = notes_file.read_text(encoding="utf-8").strip()
        if notes_text:
            _instr_markers = (
                "you must return", "you have to return", "aim for at least",
                "tools must have been called", "return a structured list",
                "at least 6 distinct",
            )
            real_notes = []
            for note in notes_text.split("\n\n---\n\n"):
                note = note.strip()
                note = note.removeprefix("## Note").strip()
                if note.startswith(("1\n", "2\n", "3\n")):
                    note = note.split("\n", 1)[-1].strip()
                note = note.removeprefix("[Auto-extracted from agent output]").strip()
                if not note:
                    continue
                lower = note.lower()
                if sum(1 for m in _instr_markers if m in lower) >= 2:
                    continue
                if len(note) < 30:
                    continue
                real_notes.append(note)
            if real_notes:
                html_parts.append('<div class="section section-break">')
                html_parts.append('<div class="section-title">Research Notes</div>')
                for note in real_notes:
                    md.reset()
                    html_parts.append(f'<div class="note-card">{md.convert(note)}</div>')
                html_parts.append('</div>')

    # Working Draft intentionally omitted — it duplicates the final report.

    # ── Sources (combined with citations) ─────────────────────────────────────
    sources_file = art_dir / "sources.json"
    if "sources" in sections and sources_file.exists():
        try:
            sources = _json.loads(sources_file.read_text(encoding="utf-8"))
            if sources:
                # Sort: cited sources first (by citation number), then uncited
                def _src_sort_key(s: dict) -> tuple:
                    norm = s.get("url", "").rstrip("/")
                    n = url_to_n.get(norm, 0)
                    return (0 if n else 1, n, s.get("title", ""))
                sources_sorted = sorted(sources, key=_src_sort_key)

                html_parts.append('<div class="section section-break">')
                html_parts.append('<div class="section-title">Sources</div>')
                for i, src in enumerate(sources_sorted, 1):
                    url   = src.get("url", "")
                    title = src.get("title") or url
                    cat   = src.get("category") or "Unknown"
                    conf  = src.get("confirmed", False)
                    badge_cls = "badge-confirmed" if conf else "badge-found"
                    badge_lbl = "Read" if conf else "Found"
                    cite_n = url_to_n.get(url.rstrip("/"), 0)
                    cite_badge = (
                        f'<span class="badge" style="background:#1e1b4b;color:#a5b4fc;'
                        f'margin-right:4pt;font-size:7pt">[{cite_n}]</span>'
                        if cite_n else ""
                    )
                    html_parts.append(
                        f'<div class="source-row">'
                        f'<span style="color:#6a6a90;min-width:18pt">{i}.</span>'
                        f'<div><div style="font-weight:600;color:#1a1a2e">'
                        f'{cite_badge}{_xml_escape(title[:120])}</div>'
                        f'<div style="color:#6366f1;font-size:8.5pt;word-break:break-all">{_xml_escape(url)}</div>'
                        f'<div style="margin-top:2pt">'
                        f'<span class="badge" style="background:#ede9fe;color:#5b21b6">{_xml_escape(cat)}</span> '
                        f'<span class="badge {badge_cls}">{badge_lbl}</span>'
                        f'</div></div></div>'
                    )
                html_parts.append('</div>')
        except Exception:
            pass

    # ── Run Stats ─────────────────────────────────────────────────────────────
    if "stats" in sections and meta:
        try:
            elapsed = meta.get("elapsed_seconds")
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed else "—"
            html_parts.append('<div class="section section-break">')
            html_parts.append('<div class="section-title">Run Statistics</div>')
            stats = [
                ("Model",          meta.get("model", "—")),
                ("Searches run",   str(meta.get("search_count", "—"))),
                ("Unique queries", str(meta.get("unique_query_count", "—"))),
                ("Pages fetched",  str(meta.get("fetch_count", "—"))),
                ("Sources found",  str(meta.get("unique_url_count", "—"))),
                ("Notes recorded", str(meta.get("note_count", "—"))),
                ("Plan updates",   str(meta.get("plan_updates", "—"))),
                ("Draft updates",  str(meta.get("draft_updates", "—"))),
                ("Report words",   str(meta.get("word_count", "—"))),
                ("Elapsed time",   elapsed_str),
            ]
            html_parts.append('<table><tbody>')
            for label, value in stats:
                html_parts.append(
                    f'<tr><td style="font-weight:600;width:40%">{label}</td><td>{value}</td></tr>'
                )
            html_parts.append('</tbody></table>')
            html_parts.append('</div>')
        except Exception:
            pass

    # ── Branch Map (rendered as SVG image) ───────────────────────────────────
    audit_file = art_dir / "audit.jsonl"
    if "tree" in sections and audit_file.exists():
        try:
            audit_events: list[dict] = []
            for line in audit_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        audit_events.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        pass
            tree = _build_research_tree(audit_events, query=query)
            if tree.get("children"):
                svg = _build_tree_svg(tree)
                html_parts.append('<div class="section section-break">')
                html_parts.append('<div class="section-title">Research Branch Map</div>')
                html_parts.append(
                    '<p style="font-size:9pt;color:#6a6a90;margin-bottom:10pt">'
                    'How the research expanded from the original query. '
                    'Indentation shows search → result → fetch → note relationships.</p>'
                )
                html_parts.append(svg)
                html_parts.append('</div>')
        except Exception:
            pass

    html_parts.append('</body></html>')
    return "".join(html_parts)


_TREE_NODE_LABELS = {
    "query":  ("🔍", "#6366f1"),
    "search": ("⚡", "#38bdf8"),
    "url":    ("🔗", "#34d399"),
    "fetch":  ("📄", "#fbbf24"),
    "note":   ("📝", "#c084fc"),
}


def _render_tree_html(node: dict, depth: int = 0) -> str:
    """Recursively render a research tree node as indented HTML."""
    if depth > 6:  # cap visual depth
        return ""
    ntype  = node.get("type", "")
    label  = node.get("label", "")
    url    = node.get("url", "")
    icon, color = _TREE_NODE_LABELS.get(ntype, ("•", "#6a6a90"))
    indent = depth * 18

    # Build this node's row
    url_span = f'<span style="display:block;font-size:8pt;color:#6366f1;word-break:break-all">{url}</span>' if url and ntype in ("url", "fetch") else ""
    confirmed = node.get("confirmed")
    badge = ""
    if ntype == "url":
        badge = ' <span style="font-size:7pt;padding:1pt 4pt;border-radius:8pt;background:#dcfce7;color:#166534">Read</span>' if confirmed else \
                ' <span style="font-size:7pt;padding:1pt 4pt;border-radius:8pt;background:#ede9fe;color:#5b21b6">Found</span>'

    row = (
        f'<div style="margin-left:{indent}pt;padding:2pt 0;line-height:1.4">'
        f'<span style="color:{color};margin-right:4pt">{icon}</span>'
        f'<span style="font-size:9.5pt;color:#1a1a2e">{label[:120]}</span>{badge}'
        f'{url_span}</div>'
    )

    children_html = "".join(
        _render_tree_html(child, depth + 1)
        for child in node.get("children", [])
    )
    return row + children_html


def _html_to_pdf(html: str) -> bytes:
    """Render HTML to PDF bytes via weasyprint."""
    import weasyprint as _wp
    return _wp.HTML(string=html).write_pdf()


@app.get("/api/reports/{filename}/export")
async def export_report(
    filename: str,
    format: str = "pdf",
    sections: str = ",".join(_ALL_SECTIONS),
) -> Response:
    """Download a full run export as PDF.

    Query params:
      format   — ``pdf`` only for now
      sections — comma-separated list of artifact sections to include.
                 Valid values: plan, notes, sources, stats, tree.
                 The Research Report is always included.
                 Default: all sections.
    """
    filename = Path(filename).name
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    report_path = REPORTS_DIR / filename
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Report '{filename}' not found.")

    report_md = report_path.read_text(encoding="utf-8")
    stem      = filename[:-3]
    art_dir   = _art_dir(filename)

    # Parse sections param into a set
    sections_set = {s.strip().lower() for s in sections.split(",") if s.strip()}

    # Pull query from meta.json for the cover page title
    query = ""
    meta_f = art_dir / "meta.json"
    if meta_f.exists():
        try:
            query = _json.loads(meta_f.read_text(encoding="utf-8")).get("query", "")
        except Exception:
            pass
    if not query:
        lines = report_md.splitlines()
        if len(lines) > 1 and lines[0].strip() == "# Query":
            query = lines[1].strip()

    if format == "pdf":
        try:
            html = _build_export_html(
                report_md,
                art_dir if art_dir.exists() else None,
                query=query,
                sections=sections_set,
            )
            pdf_bytes = _html_to_pdf(html)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{format}'. Use 'pdf'.",
        )


# ── Run artifacts ─────────────────────────────────────────────────────────────


def _art_dir(filename: str) -> Path:
    """Return the artifacts directory for a given report filename."""
    return REPORTS_DIR / Path(filename).stem


class ArtifactManifest(BaseModel):
    prefix:    str
    available: list[str]   # names of artifact files present, e.g. ["meta.json", "plan.md"]


@app.get("/api/reports/{filename}/artifacts", response_model=ArtifactManifest)
async def report_artifacts(filename: str) -> ArtifactManifest:
    """List which artifact files exist for a report."""
    filename = Path(filename).name
    if not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    art = _art_dir(filename)
    if not art.exists():
        return ArtifactManifest(prefix=Path(filename).stem, available=[])
    available = sorted(f.name for f in art.iterdir() if f.is_file())
    return ArtifactManifest(prefix=art.name, available=available)


@app.get("/api/reports/{filename}/meta")
async def report_meta(filename: str) -> dict:
    """Return the run metadata (meta.json) for a report."""
    filename = Path(filename).name
    meta_file = _art_dir(filename) / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="No meta.json for this report.")
    return _json.loads(meta_file.read_text(encoding="utf-8"))


@app.get("/api/reports/{filename}/evaluate")
async def report_evaluate(filename: str) -> dict:
    """Run heuristic evaluation on a completed report and return score + suggestions."""
    from evaluator import load_and_evaluate
    filename = Path(filename).name
    result = load_and_evaluate(_art_dir(filename))
    if result is None:
        raise HTTPException(status_code=404, detail="No meta.json found — artifacts were not saved for this run.")
    return result


@app.get("/api/reports/{filename}/plan")
async def report_plan(filename: str) -> dict:
    """Return the researcher's plan (plan.md) for a report."""
    filename = Path(filename).name
    f = _art_dir(filename) / "plan.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No plan captured for this run.")
    return {"content": f.read_text(encoding="utf-8")}


@app.get("/api/reports/{filename}/notes")
async def report_notes(filename: str) -> dict:
    """Return all research notes (notes.md) for a report."""
    filename = Path(filename).name
    f = _art_dir(filename) / "notes.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No notes captured for this run.")
    return {"content": f.read_text(encoding="utf-8")}


@app.get("/api/reports/{filename}/draft")
async def report_draft(filename: str) -> dict:
    """Return the working draft (draft.md) for a report."""
    filename = Path(filename).name
    f = _art_dir(filename) / "draft.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No draft captured for this run.")
    return {"content": f.read_text(encoding="utf-8")}


@app.get("/api/reports/{filename}/sources")
async def report_sources(filename: str) -> dict:
    """Return the sources list (sources.json) for a report."""
    filename = Path(filename).name
    f = _art_dir(filename) / "sources.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No sources captured for this run.")
    return {"sources": _json.loads(f.read_text(encoding="utf-8"))}


@app.get("/api/reports/{filename}/audit")
async def report_audit(filename: str, limit: int = 500) -> dict:
    """Return raw stream events (audit.jsonl) for a report, newest first."""
    filename = Path(filename).name
    f = _art_dir(filename) / "audit.jsonl"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No audit log for this run.")
    events: list[dict] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass
    return {"events": events[-limit:], "total": len(events)}


def _build_research_tree(events: list[dict], query: str = "") -> dict:
    """Convert a flat list of audit stream events into a nested research tree."""
    root: dict = {"id": "root", "type": "query", "label": query or "Research Session", "children": []}

    url_to_node:       dict[str, dict] = {}
    url_to_fetch_node: dict[str, dict] = {}
    current_search:    dict | None     = None
    last_fetch_node:   dict | None     = None
    note_idx   = 0
    search_idx = 0
    total      = 1

    for ev in events:
        if total > 200:
            root["truncated"] = True
            break
        t = ev.get("type", "")

        if t == "search":
            q = ev.get("query", "")
            if not q:
                continue
            node: dict = {
                "id": f"search-{search_idx}",
                "type": "search",
                "label": q,
                "agent": ev.get("agent", ""),
                "children": [],
            }
            search_idx += 1
            total += 1
            root["children"].append(node)
            current_search   = node
            last_fetch_node  = None

        elif t == "search_result":
            if current_search is None:
                continue
            for r in ev.get("results", []):
                url = r.get("url", "")
                if not url or url in url_to_node:
                    continue
                url_node: dict = {
                    "id":        f"url-{abs(hash(url)) % 0xFFFFFF:06x}",
                    "type":      "url",
                    "label":     (r.get("title") or url)[:80],
                    "url":       url,
                    "category":  r.get("category", ""),
                    "confirmed": False,
                    "children":  [],
                }
                total += 1
                current_search["children"].append(url_node)
                url_to_node[url] = url_node

        elif t == "fetch":
            url = ev.get("url", "")
            if not url:
                continue
            parent = url_to_node.get(url)
            if parent is None:
                # Analyst fetching a URL not found via search — attach to current search or root
                parent_branch = current_search or root
                url_node = {
                    "id":        f"url-{abs(hash(url)) % 0xFFFFFF:06x}",
                    "type":      "url",
                    "label":     url[:80],
                    "url":       url,
                    "category":  ev.get("category", ""),
                    "confirmed": False,
                    "children":  [],
                }
                total += 1
                parent_branch["children"].append(url_node)
                url_to_node[url] = url_node
                parent = url_node
            fetch_node: dict = {
                "id":       f"fetch-{abs(hash(url)) % 0xFFFFFF:06x}",
                "type":     "fetch",
                "label":    "Page read",
                "url":      url,
                "children": [],
            }
            total += 1
            parent["children"].append(fetch_node)
            url_to_fetch_node[url] = fetch_node
            last_fetch_node = fetch_node

        elif t == "fetch_content":
            url = ev.get("url", "")
            if url in url_to_node:
                url_to_node[url]["confirmed"] = True

        elif t == "note_add":
            content = ev.get("content", "")
            if not content:
                continue
            # Strip auto-extraction prefix
            stripped = content.removeprefix("[Auto-extracted from agent output]").strip()
            if not stripped or len(stripped) < 20:
                continue
            # Skip notes that are instruction echo (LLM repeated task expected_output)
            _skip_phrases = (
                "you must return", "you have to return", "aim for at least",
                "tools must have been called", "return a structured list",
                "at least 6 distinct", "add_note tool must", "update_draft tool must",
            )
            low = stripped.lower()
            if any(ph in low[:200] for ph in _skip_phrases):
                continue
            display = stripped[:90] + ("…" if len(stripped) > 90 else "")
            note_node: dict = {
                "id":      f"note-{note_idx}",
                "type":    "note",
                "label":   display,
                "content": stripped[:500],
                "children": [],
            }
            note_idx += 1
            total += 1
            (last_fetch_node or current_search or root)["children"].append(note_node)

    return root


@app.get("/api/reports/{filename}/tree")
async def report_tree(filename: str) -> dict:
    """Build a research branching tree from the run's audit events."""
    filename = Path(filename).name
    f = _art_dir(filename) / "audit.jsonl"
    if not f.exists():
        raise HTTPException(status_code=404, detail="No audit log for this run.")
    events: list[dict] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass
    # Pull query from meta.json if available
    query = ""
    meta_f = _art_dir(filename) / "meta.json"
    if meta_f.exists():
        try:
            query = _json.loads(meta_f.read_text(encoding="utf-8")).get("query", "")
        except Exception:
            pass
    return {"tree": _build_research_tree(events, query=query)}


# ── Settings & Discovery ───────────────────────────────────────────────────

_ENV_FILE = BASE_DIR / ".env"

# Known local AI server ports to probe
_DISCOVERY_PORTS = [1234, 11434, 8080, 8000, 1111, 4891, 5001, 7860]

# Known endpoint types by port / path signature
_ENDPOINT_NAMES = {
    1234:  "LM Studio",
    11434: "Ollama",
    8080:  "Local AI",
    1111:  "Local AI",
    4891:  "LM Studio (legacy)",
}


def _local_ips() -> list[str]:
    """Return all local IP addresses plus localhost."""
    ips: list[str] = ["localhost", "127.0.0.1"]
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=2)
        ips += [ip.strip() for ip in out.split() if ip.strip()]
    except Exception:
        pass
    try:
        ips.append(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    # de-dup, keep order
    seen: set[str] = set()
    unique = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            unique.append(ip)
    return unique


def _probe_endpoint(base_url: str, port: int) -> dict | None:
    """Try to fetch /v1/models or /api/tags from base_url. Returns endpoint dict or None."""
    import requests as _req
    # OpenAI-compatible /v1/models
    for path in ["/v1/models", "/api/tags"]:
        try:
            r = _req.get(base_url.rstrip("/") + path, timeout=1.5)
            if r.ok:
                data = r.json()
                # OpenAI format: {"data": [{"id": ...}]}
                models = [m["id"] for m in data.get("data", []) if m.get("id")]
                # Ollama format: {"models": [{"name": ...}]}
                if not models:
                    models = [m.get("name") or m.get("id") for m in data.get("models", []) if m.get("name") or m.get("id")]
                label = _ENDPOINT_NAMES.get(port, "Local AI")
                return {
                    "url": base_url,
                    "label": label,
                    "models": models,
                    "online": True,
                }
        except Exception:
            continue
    return None


class SettingsResponse(BaseModel):
    base_url: str
    model: str
    search_backend: str
    langsearch_api_key: str
    brave_api_key: str
    serpapi_key: str
    max_search_results: int
    max_page_content_length: int
    context_limit_tokens: int


class SettingsUpdateRequest(BaseModel):
    base_url: str = Field(..., min_length=5)
    model: str     = Field(..., min_length=1)
    search_backend: str = Field(default="duckduckgo")
    langsearch_api_key: str = Field(default="")
    brave_api_key: str = Field(default="")
    serpapi_key: str = Field(default="")
    max_search_results: int = Field(default=5, ge=1, le=30)
    max_page_content_length: int = Field(default=4000, ge=500, le=50000)
    context_limit_tokens: int = Field(default=256000, ge=16000, le=1000000)


class DiscoveredEndpoint(BaseModel):
    url: str
    label: str
    models: list[str]
    online: bool


class DiscoverResponse(BaseModel):
    endpoints: list[DiscoveredEndpoint]


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    """Return the current connection, search, and research behaviour settings."""
    return SettingsResponse(
        base_url=os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),
        model=os.environ.get("LM_STUDIO_MODEL", "local-model"),
        search_backend=os.environ.get("SEARCH_BACKEND", "duckduckgo"),
        langsearch_api_key=os.environ.get("LANGSEARCH_API_KEY", ""),
        brave_api_key=os.environ.get("BRAVE_API_KEY", ""),
        serpapi_key=os.environ.get("SERPAPI_KEY", ""),
        max_search_results=int(os.environ.get("MAX_SEARCH_RESULTS", "5")),
        max_page_content_length=int(os.environ.get("MAX_PAGE_CONTENT_LENGTH", "4000")),
        context_limit_tokens=int(os.environ.get("CONTEXT_LIMIT_TOKENS", "256000")),
    )


@app.post("/api/settings", response_model=SettingsResponse)
async def update_settings(body: SettingsUpdateRequest) -> SettingsResponse:
    """Persist new connection and search settings to .env and update the live environment."""
    # Update os.environ immediately (affects next spawned worker)
    os.environ["LM_STUDIO_BASE_URL"]       = body.base_url
    os.environ["LM_STUDIO_MODEL"]          = body.model
    os.environ["SEARCH_BACKEND"]           = body.search_backend
    os.environ["LANGSEARCH_API_KEY"]       = body.langsearch_api_key
    os.environ["BRAVE_API_KEY"]            = body.brave_api_key
    os.environ["SERPAPI_KEY"]              = body.serpapi_key
    os.environ["MAX_SEARCH_RESULTS"]       = str(body.max_search_results)
    os.environ["MAX_PAGE_CONTENT_LENGTH"]  = str(body.max_page_content_length)
    os.environ["CONTEXT_LIMIT_TOKENS"]     = str(body.context_limit_tokens)

    # Persist to .env file
    _write_env_key("LM_STUDIO_BASE_URL",      body.base_url)
    _write_env_key("LM_STUDIO_MODEL",         body.model)
    _write_env_key("SEARCH_BACKEND",          body.search_backend)
    _write_env_key("LANGSEARCH_API_KEY",      body.langsearch_api_key)
    _write_env_key("BRAVE_API_KEY",           body.brave_api_key)
    _write_env_key("SERPAPI_KEY",             body.serpapi_key)
    _write_env_key("MAX_SEARCH_RESULTS",      str(body.max_search_results))
    _write_env_key("MAX_PAGE_CONTENT_LENGTH", str(body.max_page_content_length))
    _write_env_key("CONTEXT_LIMIT_TOKENS",    str(body.context_limit_tokens))

    return SettingsResponse(
        base_url=body.base_url,
        model=body.model,
        search_backend=body.search_backend,
        langsearch_api_key=body.langsearch_api_key,
        brave_api_key=body.brave_api_key,
        serpapi_key=body.serpapi_key,
        max_search_results=body.max_search_results,
        max_page_content_length=body.max_page_content_length,
        context_limit_tokens=body.context_limit_tokens,
    )


def _write_env_key(key: str, value: str) -> None:
    """Update or append a key=value line in the .env file."""
    try:
        text = _ENV_FILE.read_text(encoding="utf-8") if _ENV_FILE.exists() else ""
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        new_line = f"{key}={value}"
        if pattern.search(text):
            text = pattern.sub(new_line, text)
        else:
            text = text.rstrip("\n") + f"\n{new_line}\n"
        _ENV_FILE.write_text(text, encoding="utf-8")
    except Exception:
        pass


@app.get("/api/discover", response_model=DiscoverResponse)
async def discover_endpoints() -> DiscoverResponse:
    """Scan common local ports for running AI servers and return their models."""
    ips = _local_ips()
    candidates: list[tuple[str, int]] = []
    for ip in ips:
        for port in _DISCOVERY_PORTS:
            prefix = "http"
            candidates.append((f"{prefix}://{ip}:{port}", port))

    # Remove obvious dupes (localhost == 127.0.0.1 on same port)
    seen_ports: dict[int, set[str]] = {}
    deduped: list[tuple[str, int]] = []
    for url, port in candidates:
        ip_part = url.split("://")[1].split(":")[0]
        norm = "127.0.0.1" if ip_part in ("localhost", "127.0.0.1") else ip_part
        if port not in seen_ports:
            seen_ports[port] = set()
        if norm not in seen_ports[port]:
            seen_ports[port].add(norm)
            deduped.append((url, port))

    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, _probe_endpoint, url, port)
        for url, port in deduped
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    endpoints = [
        DiscoveredEndpoint(**r)
        for r in results
        if isinstance(r, dict) and r
    ]

    # Always include current configured endpoint if not already in list
    current_url = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
    current_base = current_url.rstrip("/").removesuffix("/v1")
    if not any(e.url == current_base or e.url + "/v1" == current_url for e in endpoints):
        endpoints.insert(0, DiscoveredEndpoint(
            url=current_base, label="Current (configured)", models=[], online=False
        ))

    return DiscoverResponse(endpoints=endpoints)


@app.get("/health")
async def health():
    return {"status": "ok", "service": MCP_SERVER_NAME, "version": "2.0.0"}


# ── Static files + SPA root ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── Entry point ────────────────────────────────────────────────────────────


def start() -> None:
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    start()
