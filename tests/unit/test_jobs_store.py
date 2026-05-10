"""Unit tests for :class:`unlimited_mcp.jobs.store.JobStore`."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unlimited_mcp.jobs.result import (
    CommandRecord,
    ErrorBlock,
    JobResult,
)
from unlimited_mcp.jobs.store import JobStore


def _now() -> datetime:
    return datetime.now(UTC)


def test_make_job_id_sortable_and_unique() -> None:
    a = JobStore.make_job_id("delegate")
    b = JobStore.make_job_id("delegate")
    assert a != b
    assert a.startswith("delegate-")
    # Sort order is timestamp-based (a was made first, b second).
    assert min(a, b) == a


def test_make_job_id_sanitizes_tool() -> None:
    job_id = JobStore.make_job_id("run/command:weird")
    assert "/" not in job_id and ":" not in job_id
    assert job_id.startswith("run_command_weird-")


def test_create_idempotent(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job_id = "test-1"
    d1 = store.create(job_id)
    d2 = store.create(job_id)
    assert d1 == d2 == tmp_path / job_id
    assert (d1 / "questions").is_dir()
    assert (d1 / "artifacts").is_dir()


def test_write_and_read_result_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    now = _now()
    result = JobResult(
        ok=True,
        job_id="delegate-001",
        status="completed",
        tool="delegate_to_agent",
        started_at=now,
        finished_at=now,
        duration_ms=42,
        summary="Added a docstring",
        changed_files=["foo.py"],
        commands_run=[
            CommandRecord(
                argv=["aider", "--no-git"],
                cwd="/tmp/work",
                exit_code=0,
                safety_class="mutating",
                risk_level="low",
            )
        ],
    )
    store.write_result(result)

    loaded = store.read_result("delegate-001")
    assert loaded is not None
    assert loaded.ok is True
    assert loaded.summary == "Added a docstring"
    assert loaded.commands_run[0].safety_class == "mutating"
    # Datetimes survive JSON roundtrip (pydantic handles encoding).
    assert loaded.started_at == now


def test_read_result_returns_none_when_missing(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    assert store.read_result("does-not-exist") is None


def test_write_and_read_meta(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    meta = {
        "tool": "delegate_to_agent",
        "agent": "aider_local",
        "host": "local",
        "prompt_hash": "deadbeef",
    }
    store.write_meta("j-1", meta)
    assert store.read_meta("j-1") == meta


def test_read_meta_missing_returns_none(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    assert store.read_meta("nope") is None


def test_tail_output_returns_last_bytes(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job_id = "tail-1"
    store.create(job_id)
    store.stdout_path(job_id).write_bytes(b"a" * 100 + b"STDOUT_END")
    store.stderr_path(job_id).write_bytes(b"b" * 50 + b"STDERR_END")

    tail = store.tail_output(job_id, max_bytes=20)
    # stderr is consumed first, up to 20 bytes from its tail.
    assert b"STDERR_END" in tail
    # stdout was not consumed because budget ran out.
    assert b"STDOUT_END" not in tail


def test_tail_output_handles_missing_files(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    # Job dir doesn't even exist.
    assert store.tail_output("ghost") == b""


def test_tail_output_combines_when_budget_remains(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job_id = "tail-2"
    store.create(job_id)
    store.stdout_path(job_id).write_bytes(b"hello")
    store.stderr_path(job_id).write_bytes(b"warn")

    tail = store.tail_output(job_id, max_bytes=4096)
    # Both included, stderr first.
    assert tail == b"warn\nhello"


def test_list_jobs_sorted(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    for jid in ("z-job", "a-job", "m-job"):
        store.create(jid)
    assert store.list_jobs() == ["a-job", "m-job", "z-job"]


def test_list_jobs_empty(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "missing")
    assert store.list_jobs() == []


def test_cleanup_removes_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.create("kill-me")
    assert (tmp_path / "kill-me").exists()
    store.cleanup("kill-me")
    assert not (tmp_path / "kill-me").exists()


def test_cleanup_missing_is_noop(tmp_path: Path) -> None:
    JobStore(tmp_path).cleanup("never-existed")  # must not raise


def test_cleanup_older_than(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.create("old")
    store.create("fresh")
    # Backdate "old" by 30 days.
    old_ts = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(tmp_path / "old", (old_ts, old_ts))

    removed = store.cleanup_older_than(days=7)
    assert removed == 1
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "fresh").exists()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    now = _now()
    store.write_result(
        JobResult(
            ok=False,
            job_id="atomic-1",
            status="failed",
            tool="run_command",
            started_at=now,
            error=ErrorBlock(code="X", message="boom", hint="hint"),
        )
    )
    # No leftover .tmp files in the job dir.
    leftovers = list((tmp_path / "atomic-1").glob(".result.json.*.tmp"))
    assert leftovers == []


def test_failed_result_with_error_block_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    now = _now()
    result = JobResult(
        ok=False,
        job_id="fail-1",
        status="failed",
        tool="run_command",
        started_at=now,
        finished_at=now,
        summary="aider exited 2",
        error=ErrorBlock(
            code="AGENT_FAILED",
            message="exit 2",
            hint="Run `aider --help` to see required flags",
            retryable=False,
        ),
    )
    store.write_result(result)
    loaded = store.read_result("fail-1")
    assert loaded is not None
    assert loaded.error is not None
    assert loaded.error.code == "AGENT_FAILED"
    assert loaded.error.hint  # silent-failure UX: hint must be set


def test_make_job_id_includes_timestamp_in_iso_basic_form() -> None:
    """ID format is `tool-YYYYMMDDTHHMMSSZ-rand`. Verify timestamp shape."""
    job_id = JobStore.make_job_id("t")
    # Split safely: tool can contain '-', so split from the right.
    rest, _suffix = job_id.rsplit("-", 1)
    _tool, ts = rest.split("-", 1)
    assert len(ts) == len("YYYYMMDDTHHMMSSZ")
    assert ts.endswith("Z")
    assert ts[8] == "T"


@pytest.mark.parametrize("max_bytes", [0, 1, 10, 1024])
def test_tail_output_respects_budget(tmp_path: Path, max_bytes: int) -> None:
    store = JobStore(tmp_path)
    store.create("budget")
    store.stderr_path("budget").write_bytes(b"x" * 200)
    out = store.tail_output("budget", max_bytes=max_bytes)
    # Combined newline-separator can add 1 byte if both files contributed,
    # but here only stderr does, so length is bounded by max_bytes.
    assert len(out) <= max_bytes


def test_jobid_and_creation_with_real_make(tmp_path: Path) -> None:
    """End-to-end: make_job_id + create + roundtrip."""
    store = JobStore(tmp_path)
    job_id = JobStore.make_job_id("delegate_to_agent")
    store.create(job_id)
    assert (tmp_path / job_id).is_dir()

    # write a tiny stdout, then tail it back
    store.stdout_path(job_id).write_bytes(b"ran fine")
    assert store.tail_output(job_id) == b"ran fine"

    # mtime sanity: the dir should be very recent.
    mtime = datetime.fromtimestamp((tmp_path / job_id).stat().st_mtime, tz=UTC)
    assert (datetime.now(UTC) - mtime) < timedelta(seconds=5)
    # avoid lint complaints about unused import
    _ = time.time()
