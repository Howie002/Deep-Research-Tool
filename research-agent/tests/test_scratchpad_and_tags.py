"""
Tests for TES-34:
  Part 1 — Scratchpad class isolation (per-job instance, no shared state)
  Part 2 — Report tagging endpoints (POST/GET /api/reports/{filename}/tags)
           and tag filter on GET /api/reports
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import api_server
from scratchpad import Scratchpad


# ── Part 1: Scratchpad isolation ──────────────────────────────────────────────


def test_scratchpad_instances_are_isolated(tmp_path):
    """Two Scratchpad instances for different job IDs write to separate log files."""
    sp1 = Scratchpad("job-aaa", tmp_path)
    sp2 = Scratchpad("job-bbb", tmp_path)

    sp1.log("message from job aaa")
    sp2.log("message from job bbb")

    log1 = (tmp_path / "job-aaa.log").read_text()
    log2 = (tmp_path / "job-bbb.log").read_text()

    assert "job aaa" in log1
    assert "job bbb" not in log1
    assert "job bbb" in log2
    assert "job aaa" not in log2


def test_scratchpad_same_job_appends(tmp_path):
    """Multiple log() calls on the same instance append to the same file."""
    sp = Scratchpad("job-append", tmp_path)
    sp.log("first")
    sp.log("second")

    lines = (tmp_path / "job-append.log").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"
    assert json.loads(lines[1])["message"] == "second"


def test_scratchpad_agent_field(tmp_path):
    """agent kwarg is included in the log entry."""
    sp = Scratchpad("job-agent", tmp_path)
    sp.log("doing research", agent="Research Specialist")

    entry = json.loads((tmp_path / "job-agent.log").read_text())
    assert entry["agent"] == "Research Specialist"
    assert entry["message"] == "doing research"


# ── Part 2: Report tagging via API ────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    reports = tmp_path / "reports"
    jobs.mkdir()
    reports.mkdir()
    monkeypatch.setattr(api_server, "JOBS_DIR", jobs)
    monkeypatch.setattr(api_server, "REPORTS_DIR", reports)
    with TestClient(api_server.app, raise_server_exceptions=True) as c:
        yield c, reports


def _make_report(reports: Path, name: str = "20260401_120000_test.md") -> str:
    (reports / name).write_text("# Query\ntest query\n\nreport content")
    return name


def test_post_tags_replaces_tag_list(client):
    """POST /api/reports/{filename}/tags replaces the tag list and returns it."""
    c, reports = client
    name = _make_report(reports)

    # Set initial tags
    resp = c.post(f"/api/reports/{name}/tags", json={"tags": ["ai", "finance"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == name
    assert data["tags"] == ["ai", "finance"]

    # Replace with a new list
    resp2 = c.post(f"/api/reports/{name}/tags", json={"tags": ["health"]})
    assert resp2.status_code == 200
    assert resp2.json()["tags"] == ["health"]


def test_get_tags_returns_current_tags(client):
    """GET /api/reports/{filename}/tags returns previously set tags."""
    c, reports = client
    name = _make_report(reports)

    c.post(f"/api/reports/{name}/tags", json={"tags": ["ml", "research"]})
    resp = c.get(f"/api/reports/{name}/tags")
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["ml", "research"]


def test_get_tags_empty_when_no_tags_set(client):
    """GET /api/reports/{filename}/tags returns empty list when no tags have been set."""
    c, reports = client
    name = _make_report(reports)

    resp = c.get(f"/api/reports/{name}/tags")
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_post_tags_invalid_format_returns_400(client):
    """POST with tags containing invalid characters returns 400."""
    c, reports = client
    name = _make_report(reports)

    resp = c.post(f"/api/reports/{name}/tags", json={"tags": ["UPPERCASE", "valid"]})
    assert resp.status_code == 400

    resp2 = c.post(f"/api/reports/{name}/tags", json={"tags": ["has space"]})
    assert resp2.status_code == 400

    resp3 = c.post(f"/api/reports/{name}/tags", json={"tags": ["a" * 33]})
    assert resp3.status_code == 400


def test_post_tags_too_many_returns_400(client):
    """POST with more than 10 tags returns 400."""
    c, reports = client
    name = _make_report(reports)

    too_many = [f"tag{i}" for i in range(11)]
    resp = c.post(f"/api/reports/{name}/tags", json={"tags": too_many})
    assert resp.status_code == 400


def test_tag_filter_on_list_reports(client):
    """GET /api/reports?tags=ai returns only reports tagged with 'ai'."""
    c, reports = client
    name_tagged = _make_report(reports, "20260401_120000_tagged.md")
    name_other = _make_report(reports, "20260401_130000_other.md")

    c.post(f"/api/reports/{name_tagged}/tags", json={"tags": ["ai", "finance"]})
    c.post(f"/api/reports/{name_other}/tags", json={"tags": ["health"]})

    resp = c.get("/api/reports?tags=ai")
    assert resp.status_code == 200
    data = resp.json()
    filenames = [r["filename"] for r in data["reports"]]
    assert name_tagged in filenames
    assert name_other not in filenames


def test_tag_filter_no_matches_returns_empty(client):
    """GET /api/reports?tags=nonexistent returns empty list, not an error."""
    c, reports = client
    _make_report(reports, "20260401_120000_any.md")

    resp = c.get("/api/reports?tags=nonexistent-tag")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reports"] == []
    assert data["total"] == 0


def test_post_tags_404_for_missing_report(client):
    """POST /api/reports/{filename}/tags returns 404 for a non-existent report."""
    c, _ = client
    resp = c.post("/api/reports/does_not_exist.md/tags", json={"tags": ["ai"]})
    assert resp.status_code == 404


def test_get_tags_404_for_missing_report(client):
    """GET /api/reports/{filename}/tags returns 404 for a non-existent report."""
    c, _ = client
    resp = c.get("/api/reports/does_not_exist.md/tags")
    assert resp.status_code == 404


def test_list_reports_includes_tags(client):
    """GET /api/reports includes tags on each item."""
    c, reports = client
    name = _make_report(reports)
    c.post(f"/api/reports/{name}/tags", json={"tags": ["ai"]})

    resp = c.get("/api/reports")
    assert resp.status_code == 200
    items = resp.json()["reports"]
    assert len(items) == 1
    assert items[0]["tags"] == ["ai"]
