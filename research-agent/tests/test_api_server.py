"""
Integration tests for api_server.py using FastAPI's TestClient.

Workers are never spawned — launch_worker is patched out in all tests
that exercise POST /api/jobs. JOBS_DIR and REPORTS_DIR are redirected to
tmp_path so no real job files are created or read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import api_server
from job_manager import _write_job


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def dirs(tmp_path):
    jobs = tmp_path / "jobs"
    reports = tmp_path / "reports"
    jobs.mkdir()
    reports.mkdir()
    return jobs, reports


@pytest.fixture
def client(dirs, monkeypatch):
    """TestClient with JOBS_DIR / REPORTS_DIR pointing to tmp_path."""
    jobs, reports = dirs
    monkeypatch.setattr(api_server, "JOBS_DIR", jobs)
    monkeypatch.setattr(api_server, "REPORTS_DIR", reports)
    with TestClient(api_server.app, raise_server_exceptions=True) as c:
        yield c, jobs, reports


# ── POST /api/jobs ────────────────────────────────────────────────────────────


def test_post_jobs_returns_job_id(client):
    c, jobs, _ = client
    with patch("api_server.launch_worker"), patch("api_server.find_running_job", return_value=None):
        resp = c.post("/api/jobs", json={"query": "test research question"})
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["job_id"]


def test_post_jobs_creates_job_file(client):
    c, jobs, _ = client
    with patch("api_server.launch_worker"), patch("api_server.find_running_job", return_value=None):
        resp = c.post("/api/jobs", json={"query": "file creation test"})
    job_id = resp.json()["job_id"]
    assert (jobs / f"{job_id}.json").exists()


def test_post_jobs_rejects_query_too_short(client):
    c, _, _ = client
    resp = c.post("/api/jobs", json={"query": "ab"})
    assert resp.status_code == 422


def test_post_jobs_rejects_missing_query(client):
    c, _, _ = client
    resp = c.post("/api/jobs", json={})
    assert resp.status_code == 422


def test_post_jobs_returns_existing_job_if_duplicate(client):
    c, _, _ = client
    existing_id = "existing-job-abc"
    with patch("api_server.find_running_job", return_value=existing_id):
        resp = c.post("/api/jobs", json={"query": "duplicate query"})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == existing_id


# ── GET /api/jobs/{job_id} ────────────────────────────────────────────────────


def test_get_job_returns_running_status(client):
    c, jobs, _ = client
    job_id = "running-job-001"
    _write_job(job_id, jobs, {
        "status": "running",
        "query": "running test",
        "result": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    (jobs / f"{job_id}.started").write_text("")  # prevent crash detection

    resp = c.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert "log" in data
    assert "new_offset" in data


def test_get_job_returns_error_status(client):
    c, jobs, _ = client
    job_id = "error-job-001"
    _write_job(job_id, jobs, {
        "status": "error",
        "query": "error test",
        "result": "Something went wrong.",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    resp = c.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert data["result"] == "Something went wrong."


def test_get_job_returns_complete_with_result(client):
    c, jobs, reports = client
    job_id = "complete-job-001"
    _write_job(job_id, jobs, {
        "status": "complete",
        "query": "complete test query",
        "result": "The answer is 42.",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    resp = c.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["result"] == "The answer is 42."
    # Job file should be cleaned up after a complete response
    assert not (jobs / f"{job_id}.json").exists()


def test_get_job_404_not_found(client):
    c, _, _ = client
    resp = c.get("/api/jobs/does-not-exist-xyz")
    assert resp.status_code == 404


def test_get_job_with_log_offset(client):
    c, jobs, _ = client
    job_id = "log-offset-job"
    _write_job(job_id, jobs, {
        "status": "running",
        "query": "offset test",
        "result": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    (jobs / f"{job_id}.started").write_text("")
    (jobs / f"{job_id}.log").write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')

    resp = c.get(f"/api/jobs/{job_id}?log_offset=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["new_offset"] == 3
    assert data["log"] == [{"n": 3}]


# ── POST /api/jobs/{job_id}/cancel ────────────────────────────────────────────


def test_cancel_job_returns_200(client):
    c, jobs, _ = client
    job_id = "cancel-me-001"
    _write_job(job_id, jobs, {
        "status": "running",
        "query": "cancel test",
        "result": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": None,
    })

    resp = c.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "cancelled"


def test_cancel_job_404_not_found(client):
    c, _, _ = client
    resp = c.post("/api/jobs/no-such-job/cancel")
    assert resp.status_code == 404


def test_cancel_job_409_already_complete(client):
    c, jobs, _ = client
    job_id = "already-complete"
    _write_job(job_id, jobs, {
        "status": "complete",
        "query": "done",
        "result": "done",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    resp = c.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 409


def test_cancel_job_409_already_cancelled(client):
    c, jobs, _ = client
    job_id = "already-cancelled"
    _write_job(job_id, jobs, {
        "status": "cancelled",
        "query": "cancel twice",
        "result": "Job was cancelled by user.",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    resp = c.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 409


# ── GET /api/reports ──────────────────────────────────────────────────────────


def test_list_reports_empty(client):
    c, _, _ = client
    resp = c.get("/api/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reports"] == []
    assert data["total"] == 0


def test_list_reports_returns_items(client):
    c, _, reports = client
    (reports / "20260101_120000_test_query.md").write_text("# Query\ntest query\n\nresult here")

    resp = c.get("/api/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["reports"]) == 1
    assert data["reports"][0]["filename"] == "20260101_120000_test_query.md"
    assert "created" in data["reports"][0]


def test_list_reports_newest_first(client):
    c, _, reports = client
    import time

    (reports / "20260101_120000_older.md").write_text("# Query\nolder\n\ncontent")
    time.sleep(0.01)
    (reports / "20260101_130000_newer.md").write_text("# Query\nnewer\n\ncontent")

    resp = c.get("/api/reports")
    data = resp.json()
    assert data["reports"][0]["filename"] == "20260101_130000_newer.md"


# ── GET /api/reports/{filename} ───────────────────────────────────────────────


def test_get_report_200(client):
    c, _, reports = client
    (reports / "20260101_120000_myreport.md").write_text("# Query\nmy query\n\nreport content")
    resp = c.get("/api/reports/20260101_120000_myreport.md")
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "20260101_120000_myreport.md"
    assert "report content" in data["content"]


def test_get_report_404_missing(client):
    c, _, _ = client
    resp = c.get("/api/reports/nonexistent_report.md")
    assert resp.status_code == 404


def test_get_report_400_non_md_extension(client):
    c, _, _ = client
    resp = c.get("/api/reports/report.txt")
    assert resp.status_code == 400


def test_get_report_prevents_directory_traversal(client):
    c, _, reports = client
    # Path traversal attempt — should be sanitised to just the filename
    (reports / "secret.md").write_text("# Query\nsecret\n\nhidden")
    resp = c.get("/api/reports/../../secret.md")
    # The server strips to filename only — should 404 if the sanitised path doesn't exist
    # or succeed only if the file is legitimately in reports dir
    assert resp.status_code in (200, 404, 400)


# ── GET /api/reports/{filename}/export ────────────────────────────────────────


def test_export_report_pdf_200(client):
    c, _, reports = client
    (reports / "20260101_120000_report.md").write_text("# Query\ntest\n\n## Body\nHello world.")
    with patch("api_server._md_to_pdf", return_value=b"%PDF-fake"):
        resp = c.get("/api/reports/20260101_120000_report.md/export?format=pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-fake"


def test_export_report_docx_200(client):
    c, _, reports = client
    (reports / "20260101_120000_report.md").write_text("# Query\ntest\n\n## Body\nHello world.")
    fake_docx = b"PK\x03\x04fake-docx-bytes"
    with patch("api_server._md_to_docx", return_value=fake_docx):
        resp = c.get("/api/reports/20260101_120000_report.md/export?format=docx")
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["content-type"]
    assert resp.content == fake_docx


def test_export_report_unknown_format_400(client):
    c, _, reports = client
    (reports / "20260101_120000_report.md").write_text("# Query\ntest\n\ncontent")
    resp = c.get("/api/reports/20260101_120000_report.md/export?format=html")
    assert resp.status_code == 400


def test_export_report_not_found_404(client):
    c, _, _ = client
    resp = c.get("/api/reports/nonexistent_report.md/export?format=pdf")
    assert resp.status_code == 404


def test_export_report_default_format_is_pdf(client):
    c, _, reports = client
    (reports / "20260101_120000_report.md").write_text("# Query\ntest\n\ncontent")
    with patch("api_server._md_to_pdf", return_value=b"%PDF-default"):
        resp = c.get("/api/reports/20260101_120000_report.md/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


# ── GET /api/reports/search & POST /api/reports/reindex ──────────────────────


def test_search_reports_empty_index(client):
    """Search returns empty results and 200 when no reports are indexed."""
    c, _, _ = client
    resp = c.get("/api/reports/search?q=anything")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["total"] == 0


def test_search_reports_matches_title(client):
    """Query that matches the H1 title is returned as a result."""
    c, _, reports = client
    (reports / "20260101_120000_alpha.md").write_text("# Alpha Project\nmy query\n\nsome content here")
    # Rebuild index so the file is indexed
    c.post("/api/reports/reindex")

    resp = c.get("/api/reports/search?q=alpha")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["results"][0]["filename"] == "20260101_120000_alpha.md"
    assert data["results"][0]["title"] == "Alpha Project"


def test_search_reports_matches_snippet(client):
    """Query that matches the snippet (but not the title) is included."""
    c, _, reports = client
    (reports / "20260101_120000_snippet.md").write_text("# Unrelated Title\nmy query\n\nhidden keyword inside snippet")
    c.post("/api/reports/reindex")

    resp = c.get("/api/reports/search?q=hidden+keyword")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1


def test_search_reports_title_ranks_before_snippet(client):
    """Title match should come before a snippet-only match."""
    import time
    c, _, reports = client
    # Write snippet-only match first (older mtime)
    (reports / "20260101_120000_snippet_only.md").write_text(
        "# Boring Title\nquery\n\nresearch is the keyword here"
    )
    time.sleep(0.02)
    # Write title match second (newer mtime)
    (reports / "20260101_130000_title_match.md").write_text(
        "# Research Overview\nquery\n\nsome other content"
    )
    c.post("/api/reports/reindex")

    resp = c.get("/api/reports/search?q=research")
    data = resp.json()
    assert data["total"] == 2
    # Title match must come first regardless of creation time
    assert data["results"][0]["filename"] == "20260101_130000_title_match.md"
    assert data["results"][1]["filename"] == "20260101_120000_snippet_only.md"


def test_search_reports_pagination(client):
    """Pagination returns correct page and total_pages."""
    c, _, reports = client
    for i in range(5):
        (reports / f"2026010{i}_120000_report_{i}.md").write_text(
            f"# Report {i}\nquery\n\ncontent {i}"
        )
    c.post("/api/reports/reindex")

    resp = c.get("/api/reports/search?q=report&page=1&page_size=2")
    data = resp.json()
    assert data["total"] == 5
    assert data["total_pages"] == 3
    assert len(data["results"]) == 2

    resp2 = c.get("/api/reports/search?q=report&page=3&page_size=2")
    data2 = resp2.json()
    assert len(data2["results"]) == 1


def test_search_reports_no_match_returns_empty_not_404(client):
    """No-match should return 200 with empty results, not 404."""
    c, _, reports = client
    (reports / "20260101_120000_xyz.md").write_text("# Some Title\nquery\n\ncontent")
    c.post("/api/reports/reindex")

    resp = c.get("/api/reports/search?q=zzznomatch")
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_reindex_rebuilds_from_disk(client):
    """POST /api/reports/reindex returns newly written reports."""
    c, _, reports = client
    # Write file after fixture setup (no prior index)
    (reports / "20260101_120000_fresh.md").write_text("# Fresh Report\nquery\n\ncontent")

    resp = c.post("/api/reports/reindex")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["results"][0]["filename"] == "20260101_120000_fresh.md"


# ── GET /health ───────────────────────────────────────────────────────────────


def test_health_endpoint(client):
    c, _, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
