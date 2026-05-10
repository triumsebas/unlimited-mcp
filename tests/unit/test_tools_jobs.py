"""Unit tests for tools/jobs.py — get_job_result, list_jobs, cancel_job."""

from __future__ import annotations

from pathlib import Path

import pytest

from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.tools.jobs import cancel_job, get_job_result, list_jobs

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


@pytest.fixture()
def runner(store: JobStore) -> LocalRunner:
    return LocalRunner(store)


# ---------------------------------------------------------------------------
# get_job_result
# ---------------------------------------------------------------------------


def test_get_job_result_unknown_id_returns_not_found(runner: LocalRunner) -> None:
    result = get_job_result("no-such-id", runner=runner)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"
    assert result.job_id == "no-such-id"


def test_get_job_result_finds_known_job(runner: LocalRunner) -> None:
    submitted = runner.submit(["/bin/echo", "hello"], tool="run_command")
    result = get_job_result(submitted.job_id, runner=runner)
    # The job_id must match; status may be running/completed/failed depending on timing.
    assert result.job_id == submitted.job_id
    assert result.error is None or result.error.code != "JOB_NOT_FOUND"
    runner.join_all()


def test_get_job_result_after_completion_returns_completed(runner: LocalRunner) -> None:
    submitted = runner.submit(["/bin/echo", "done"], tool="run_command")
    runner.join_all()
    result = get_job_result(submitted.job_id, runner=runner)
    assert result.status == "completed"
    assert result.ok is True


def test_get_job_result_error_hints_list_jobs(runner: LocalRunner) -> None:
    result = get_job_result("ghost", runner=runner)
    assert result.error is not None
    assert "list_jobs" in (result.error.hint or "")


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


def test_list_jobs_empty_store_returns_empty_list(runner: LocalRunner) -> None:
    assert list_jobs(runner=runner) == []


def test_list_jobs_returns_all_submitted_jobs(runner: LocalRunner) -> None:
    r1 = runner.submit(["/bin/echo", "a"], tool="run_command")
    r2 = runner.submit(["/bin/echo", "b"], tool="run_command")
    runner.join_all()

    results = list_jobs(runner=runner)
    ids = {r.job_id for r in results}
    assert r1.job_id in ids
    assert r2.job_id in ids


def test_list_jobs_shows_completed_status(runner: LocalRunner) -> None:
    runner.submit(["/bin/echo", "hi"], tool="run_command")
    runner.join_all()

    results = list_jobs(runner=runner)
    assert len(results) == 1
    assert results[0].status == "completed"


def test_list_jobs_result_count_matches_submitted(runner: LocalRunner) -> None:
    for i in range(3):
        runner.submit(["/bin/echo", str(i)], tool="run_command")
    runner.join_all()

    assert len(list_jobs(runner=runner)) == 3


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


def test_cancel_unknown_job_returns_not_found(runner: LocalRunner) -> None:
    result = cancel_job("no-such-id", runner=runner)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"


def test_cancel_completed_job_returns_existing_result(runner: LocalRunner) -> None:
    submitted = runner.submit(["/bin/echo", "done"], tool="run_command")
    runner.join_all()

    result = cancel_job(submitted.job_id, runner=runner)
    assert result.status == "completed"


def test_cancel_running_job_returns_cancelled(runner: LocalRunner) -> None:
    submitted = runner.submit(["/bin/sleep", "60"], tool="run_command")

    result = cancel_job(submitted.job_id, runner=runner)
    assert result.status == "cancelled"
    assert result.job_id == submitted.job_id
    runner.join_all()
