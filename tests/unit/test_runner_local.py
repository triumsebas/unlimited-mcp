"""Unit tests for jobs/runner_local.py.

All tests use real subprocesses (no mocking of subprocess) so that the
Popen/watcher-thread lifecycle is exercised end-to-end.  Each test calls
``runner.join_all()`` before asserting final state to avoid races.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from unlimited_mcp.jobs.runner_local import LocalRunner, _pid_alive, _read_state, _write_state
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.redactor import Redactor


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


@pytest.fixture()
def runner(store: JobStore) -> LocalRunner:
    return LocalRunner(store)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_done(runner: LocalRunner, job_id: str, timeout: float = 5.0) -> None:
    """Poll until the job leaves 'running' or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = runner.get_result(job_id)
        if r and r.status != "running":
            return
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# submit — immediate return
# ---------------------------------------------------------------------------


def test_submit_returns_running(runner: LocalRunner) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    assert result.status == "running"
    assert result.ok is False
    assert result.job_id
    runner.join_all()


def test_submit_creates_job_dir(runner: LocalRunner, store: JobStore) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    assert store.job_dir(result.job_id).exists()
    runner.join_all()


def test_submit_writes_state_json(runner: LocalRunner, store: JobStore) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    state = _read_state(store.job_dir(result.job_id) / "state.json")
    assert state is not None
    assert state["status"] == "running"
    assert isinstance(state["pid"], int)
    runner.join_all()


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def test_successful_command(runner: LocalRunner) -> None:
    result = runner.submit([sys.executable, "-c", "print('done')"])
    runner.join_all()
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.status == "completed"
    assert final.ok is True


def test_failing_command(runner: LocalRunner) -> None:
    result = runner.submit([sys.executable, "-c", "raise SystemExit(1)"])
    runner.join_all()
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert final.ok is False


def test_stdout_written_to_disk(runner: LocalRunner, store: JobStore) -> None:
    result = runner.submit([sys.executable, "-c", "print('hello from stdout')"])
    runner.join_all()
    data = store.stdout_path(result.job_id).read_bytes()
    assert b"hello from stdout" in data


def test_stderr_written_to_disk(runner: LocalRunner, store: JobStore) -> None:
    result = runner.submit([sys.executable, "-c", "import sys; sys.stderr.write('err line\\n')"])
    runner.join_all()
    data = store.stderr_path(result.job_id).read_bytes()
    assert b"err line" in data


def test_duration_ms_populated(runner: LocalRunner) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    runner.join_all()
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.duration_ms is not None
    assert final.duration_ms >= 0


def test_raw_output_ref_set(runner: LocalRunner, store: JobStore) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    runner.join_all()
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.raw_output_ref == str(store.stdout_path(result.job_id))


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_marks_failed(runner: LocalRunner) -> None:
    result = runner.submit(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
    )
    runner.join_all(timeout=5.0)
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert "timeout" in (final.summary or "").lower()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_key_deduplicates(runner: LocalRunner) -> None:
    r1 = runner.submit([sys.executable, "-c", "pass"], idempotency_key="k1")
    r2 = runner.submit([sys.executable, "-c", "pass"], idempotency_key="k1")
    assert r1.job_id == r2.job_id
    runner.join_all()


def test_idempotency_key_resubmits_after_failure(runner: LocalRunner) -> None:
    r1 = runner.submit([sys.executable, "-c", "raise SystemExit(1)"], idempotency_key="k2")
    runner.join_all()
    # After failure the key slot is reusable
    r2 = runner.submit([sys.executable, "-c", "pass"], idempotency_key="k2")
    assert r2.job_id != r1.job_id
    runner.join_all()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_applied_to_stdout(tmp_path: Path) -> None:
    secret = "supersecrettoken123"
    store = JobStore(tmp_path / "jobs")
    redactor = Redactor(environ={"MY_TOKEN": secret})
    runner = LocalRunner(store, redactor=redactor)

    result = runner.submit([sys.executable, "-c", f"print('{secret}')"])
    runner.join_all()

    data = store.stdout_path(result.job_id).read_bytes()
    assert secret.encode() not in data
    assert b"***REDACTED***" in data


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_running_job(runner: LocalRunner) -> None:
    result = runner.submit(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=60,
    )
    time.sleep(0.1)  # let the process start
    cancelled = runner.cancel(result.job_id)
    assert cancelled.status == "cancelled"
    runner.join_all(timeout=3.0)


def test_cancel_unknown_job(runner: LocalRunner) -> None:
    result = runner.cancel("nonexistent-job-id")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"


def test_cancel_completed_job_is_noop(runner: LocalRunner) -> None:
    result = runner.submit([sys.executable, "-c", "pass"])
    runner.join_all()
    final = runner.get_result(result.job_id)
    assert final is not None
    assert final.status == "completed"
    noop = runner.cancel(result.job_id)
    assert noop.status == "completed"


# ---------------------------------------------------------------------------
# Zombie detection
# ---------------------------------------------------------------------------


def test_zombie_detection(store: JobStore) -> None:
    """A job recorded as running whose PID is gone is marked failed."""
    runner = LocalRunner(store)
    job_id = JobStore.make_job_id("test")
    store.create(job_id)
    from datetime import UTC, datetime

    started_at = datetime.now(UTC)
    initial = __import__("unlimited_mcp.jobs.result", fromlist=["JobResult"]).JobResult(
        ok=False,
        job_id=job_id,
        status="running",
        tool="test",
        started_at=started_at,
    )
    store.write_result(initial)
    _write_state(
        store.job_dir(job_id) / "state.json",
        {"status": "running", "pid": 99999999, "started_at": started_at.isoformat()},
    )

    result = runner.get_result(job_id)
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_ZOMBIE"


# ---------------------------------------------------------------------------
# _pid_alive helper
# ---------------------------------------------------------------------------


def test_pid_alive_current_process() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_nonexistent() -> None:
    assert _pid_alive(99999999) is False
