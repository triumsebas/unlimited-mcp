# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Unit tests for the local task-spooler backend (TsRunner + _ts_worker).

These cover the cross-process durability fixes:

* B8 — submit↔worker ordering: a fast worker that writes a terminal
  result.json must not be clobbered by submit's initial "running" write.
* B4 (cross-process) — cancel wins: the worker must not overwrite a
  "cancelled" result written by TsRunner.cancel() in another process.

The ts binary is mocked so these run without task-spooler installed and are
fully deterministic (no real subprocess scheduling).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from unlimited_mcp.jobs import _ts_worker
from unlimited_mcp.jobs import runner_ts as rt
from unlimited_mcp.jobs.result import JobResult
from unlimited_mcp.jobs.store import JobStore, file_lock


def _completed(job_id: str) -> JobResult:
    now = datetime.now(UTC)
    return JobResult(
        ok=True, job_id=job_id, status="completed", tool="run_command",
        started_at=now, finished_at=now, summary="worker finished fast",
    )


# ---------------------------------------------------------------------------
# B8 — submit must not clobber a fast worker's terminal result
# ---------------------------------------------------------------------------


def test_submit_does_not_clobber_fast_worker(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs")

    def fake_run(cmd, *a, **kw):
        res = MagicMock(returncode=0, stdout="", stderr="")
        if "-V" in cmd or "-S" in cmd:
            return res
        # Enqueue call: simulate a worker that finishes and writes a terminal
        # result.json *during* enqueue (the B8 race window).
        job_id = cmd[cmd.index("-L") + 1]
        store.write_result(_completed(job_id))
        res.stdout = "5"  # ts slot id
        return res

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    runner = rt.TsRunner(store, ts_bin="ts")
    result = runner.submit(["echo", "hi"])

    final = store.read_result(result.job_id)
    assert final is not None
    # Without the B8 fix, submit's late "running" write clobbers this.
    assert final.status == "completed"


def test_submit_returns_running_initially(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs")

    def fake_run(cmd, *a, **kw):
        res = MagicMock(returncode=0, stdout="7", stderr="")
        return res

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    runner = rt.TsRunner(store, ts_bin="ts")
    result = runner.submit(["echo", "hi"])
    assert result.status == "running"
    # result.json exists from before the enqueue.
    assert store.read_result(result.job_id) is not None


def test_submit_failure_marks_failed(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs")

    def fake_run(cmd, *a, **kw):
        if "-V" in cmd or "-S" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="boom: queue full")

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    runner = rt.TsRunner(store, ts_bin="ts")
    result = runner.submit(["echo", "hi"])
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "TS_SUBMIT_FAILED"


# ---------------------------------------------------------------------------
# B4 cross-process — worker guard: cancel wins
# ---------------------------------------------------------------------------


def test_worker_does_not_overwrite_cancelled(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    job_id = JobStore.make_job_id("test")
    job_dir = store.create(job_id)
    now = datetime.now(UTC)
    store.write_result(
        JobResult(ok=False, job_id=job_id, status="cancelled", tool="run_command",
                  started_at=now, finished_at=now, summary="Cancelled by orchestrator.")
    )

    _ts_worker._write_result_unless_cancelled(job_dir, _completed(job_id).model_dump(mode="json"))

    final = store.read_result(job_id)
    assert final is not None
    assert final.status == "cancelled"


def test_worker_writes_result_when_not_cancelled(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    job_id = JobStore.make_job_id("test")
    job_dir = store.create(job_id)
    now = datetime.now(UTC)
    store.write_result(
        JobResult(ok=False, job_id=job_id, status="running", tool="run_command", started_at=now)
    )

    _ts_worker._write_result_unless_cancelled(job_dir, _completed(job_id).model_dump(mode="json"))

    final = store.read_result(job_id)
    assert final is not None
    assert final.status == "completed"


def test_cancel_wins_over_running_job(tmp_path: Path, monkeypatch) -> None:
    """A job still running at cancel() time becomes cancelled (cancel wins)."""
    store = JobStore(tmp_path / "jobs")

    def fake_run(cmd, *a, **kw):
        return MagicMock(returncode=0, stdout="9", stderr="")

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    runner = rt.TsRunner(store, ts_bin="ts")
    result = runner.submit(["sleep", "30"])

    cancelled = runner.cancel(result.job_id)
    assert cancelled.status == "cancelled"
    # If the worker now tries to write a completed result, the guard refuses it.
    _ts_worker._write_result_unless_cancelled(
        store.job_dir(result.job_id), _completed(result.job_id).model_dump(mode="json")
    )
    final = store.read_result(result.job_id)
    assert final is not None
    assert final.status == "cancelled"


# ---------------------------------------------------------------------------
# file_lock — cross-process exclusion primitive
# ---------------------------------------------------------------------------


def test_file_lock_serializes_writers(tmp_path: Path) -> None:
    """Two threads contending on the same lockfile never interleave the
    critical section."""
    lock_path = tmp_path / ".lock"
    order: list[str] = []
    in_section = threading.Event()
    overlap = []

    def worker(name: str) -> None:
        with file_lock(lock_path):
            if in_section.is_set():
                overlap.append(name)
            in_section.set()
            order.append(f"{name}-enter")
            time.sleep(0.05)
            order.append(f"{name}-exit")
            in_section.clear()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap == []  # no overlapping critical sections
    # Each enter is immediately followed by its own exit.
    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    )
