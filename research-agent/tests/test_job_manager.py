"""
Unit tests for job_manager.py.

Uses tmp_path for full file isolation — no real jobs/ directory is touched.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_manager import (
    STARTUP_GRACE_SECONDS,
    _write_job,
    check_job_health,
    cleanup_job,
    create_job,
    read_job,
    read_log,
    sweep_stale_jobs,
)


# ── create_job ────────────────────────────────────────────────────────────────


def test_create_job_creates_valid_json_file(tmp_path):
    job_id = create_job("test query", tmp_path)
    job_file = tmp_path / f"{job_id}.json"
    assert job_file.exists()
    data = json.loads(job_file.read_text())
    assert data["status"] == "running"
    assert data["query"] == "test query"
    assert "started_at" in data
    assert "result" in data


def test_create_job_returns_valid_uuid(tmp_path):
    import uuid

    job_id = create_job("uuid check", tmp_path)
    # Raises ValueError if not a valid UUID
    uuid.UUID(job_id)


def test_create_job_different_queries_produce_different_ids(tmp_path):
    id1 = create_job("query one", tmp_path)
    id2 = create_job("query two", tmp_path)
    assert id1 != id2


# ── read_job / _write_job roundtrip ──────────────────────────────────────────


def test_read_write_job_roundtrip(tmp_path):
    job_id = "roundtrip-001"
    payload = {"status": "running", "query": "q", "result": "", "started_at": "2026-01-01T00:00:00+00:00"}
    _write_job(job_id, tmp_path, payload)
    assert read_job(job_id, tmp_path) == payload


def test_write_job_is_atomic_no_tmp_leftover(tmp_path):
    job_id = "atomic-001"
    _write_job(job_id, tmp_path, {"x": 1})
    assert not (tmp_path / f"{job_id}.tmp").exists()
    assert (tmp_path / f"{job_id}.json").exists()


def test_write_job_overwrites_existing(tmp_path):
    job_id = "overwrite-001"
    _write_job(job_id, tmp_path, {"v": 1})
    _write_job(job_id, tmp_path, {"v": 2})
    assert read_job(job_id, tmp_path) == {"v": 2}


def test_read_job_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_job("nonexistent-job", tmp_path)


# ── read_log ──────────────────────────────────────────────────────────────────


def test_read_log_missing_file_returns_empty(tmp_path):
    entries, total = read_log("no-log-here", tmp_path)
    assert entries == []
    assert total == 0


def test_read_log_empty_file_returns_empty(tmp_path):
    job_id = "empty-log"
    (tmp_path / f"{job_id}.log").write_text("")
    entries, total = read_log(job_id, tmp_path)
    assert entries == []
    assert total == 0


def test_read_log_parses_valid_json_lines(tmp_path):
    job_id = "valid-log"
    (tmp_path / f"{job_id}.log").write_text('{"msg": "a"}\n{"msg": "b"}\n')
    entries, total = read_log(job_id, tmp_path)
    assert total == 2
    assert entries == [{"msg": "a"}, {"msg": "b"}]


def test_read_log_skips_malformed_lines(tmp_path):
    job_id = "malformed-log"
    (tmp_path / f"{job_id}.log").write_text('{"ok": true}\nNOT JSON\n{"ok": false}\n')
    entries, total = read_log(job_id, tmp_path)
    assert total == 2
    assert entries == [{"ok": True}, {"ok": False}]


def test_read_log_skips_blank_lines(tmp_path):
    job_id = "blank-lines"
    (tmp_path / f"{job_id}.log").write_text('\n{"n": 1}\n\n{"n": 2}\n\n')
    entries, total = read_log(job_id, tmp_path)
    assert total == 2


def test_read_log_offset_returns_tail(tmp_path):
    job_id = "offset-log"
    (tmp_path / f"{job_id}.log").write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    entries, total = read_log(job_id, tmp_path, log_offset=2)
    assert total == 3
    assert entries == [{"n": 3}]


def test_read_log_offset_beyond_end_returns_empty(tmp_path):
    job_id = "offset-beyond"
    (tmp_path / f"{job_id}.log").write_text('{"n": 1}\n')
    entries, total = read_log(job_id, tmp_path, log_offset=10)
    assert total == 1
    assert entries == []


# ── cleanup_job ───────────────────────────────────────────────────────────────


def test_cleanup_job_removes_all_associated_files(tmp_path):
    job_id = "cleanup-001"
    for suffix in (".json", ".log", ".started"):
        (tmp_path / f"{job_id}{suffix}").write_text("data")
    cleanup_job(job_id, tmp_path)
    for suffix in (".json", ".log", ".started"):
        assert not (tmp_path / f"{job_id}{suffix}").exists()


def test_cleanup_job_missing_files_does_not_raise(tmp_path):
    # Should succeed silently even if no files exist
    cleanup_job("ghost-job", tmp_path)


def test_cleanup_job_only_removes_own_files(tmp_path):
    job_a = "job-aaa"
    job_b = "job-bbb"
    (tmp_path / f"{job_a}.json").write_text("a")
    (tmp_path / f"{job_b}.json").write_text("b")
    cleanup_job(job_a, tmp_path)
    assert not (tmp_path / f"{job_a}.json").exists()
    assert (tmp_path / f"{job_b}.json").exists()


# ── sweep_stale_jobs ──────────────────────────────────────────────────────────


def test_sweep_stale_jobs_marks_timed_out_job_as_error(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
    job_id = create_job("stale query", tmp_path)
    data = read_job(job_id, tmp_path)
    data["started_at"] = old_ts
    _write_job(job_id, tmp_path, data)

    count = sweep_stale_jobs(tmp_path, timeout_seconds=100)

    assert count == 1
    updated = read_job(job_id, tmp_path)
    assert updated["status"] == "error"


def test_sweep_stale_jobs_ignores_completed_jobs(tmp_path):
    job_id = create_job("done query", tmp_path)
    data = read_job(job_id, tmp_path)
    data["status"] = "complete"
    _write_job(job_id, tmp_path, data)

    count = sweep_stale_jobs(tmp_path, timeout_seconds=1)
    assert count == 0


def test_sweep_stale_jobs_ignores_recent_running_jobs(tmp_path):
    job_id = create_job("fresh query", tmp_path)
    # Write a startup marker so health check won't flag it
    (tmp_path / f"{job_id}.started").write_text("")

    count = sweep_stale_jobs(tmp_path, timeout_seconds=10000)
    assert count == 0


def test_sweep_stale_jobs_returns_correct_count(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat()
    for i in range(3):
        job_id = create_job(f"stale {i}", tmp_path)
        data = read_job(job_id, tmp_path)
        data["started_at"] = old_ts
        _write_job(job_id, tmp_path, data)

    count = sweep_stale_jobs(tmp_path, timeout_seconds=100)
    assert count == 3


# ── check_job_health ──────────────────────────────────────────────────────────


def test_check_job_health_detects_startup_crash_after_grace(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=STARTUP_GRACE_SECONDS + 30)).isoformat()
    job_id = create_job("crash query", tmp_path)
    data = read_job(job_id, tmp_path)
    data["started_at"] = old_ts
    _write_job(job_id, tmp_path, data)
    # No .started marker written

    updated = check_job_health(job_id, tmp_path, data)

    assert updated["status"] == "error"
    assert "startup marker" in updated["result"]


def test_check_job_health_healthy_job_with_startup_marker(tmp_path):
    job_id = create_job("healthy query", tmp_path)
    data = read_job(job_id, tmp_path)
    (tmp_path / f"{job_id}.started").write_text("")

    updated = check_job_health(job_id, tmp_path, data)

    assert updated["status"] == "running"


def test_check_job_health_detects_timeout(tmp_path):
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
    job_id = create_job("timeout query", tmp_path)
    data = read_job(job_id, tmp_path)
    data["started_at"] = old_ts
    _write_job(job_id, tmp_path, data)

    updated = check_job_health(job_id, tmp_path, data, timeout_seconds=100)

    assert updated["status"] == "error"
    assert "timed out" in updated["result"]


def test_check_job_health_skips_non_running_jobs(tmp_path):
    job_id = create_job("done query", tmp_path)
    data = read_job(job_id, tmp_path)
    data["status"] = "complete"
    data["result"] = "done"
    _write_job(job_id, tmp_path, data)

    updated = check_job_health(job_id, tmp_path, data, timeout_seconds=1)

    assert updated["status"] == "complete"
