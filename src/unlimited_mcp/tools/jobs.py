"""Job-management tool functions: get_job_result, list_jobs, cancel_job.

These are pure Python functions — no MCP SDK import, no global state.
The server registers them as MCP tools in a later step.

All three functions accept a :class:`~unlimited_mcp.jobs.runner_local.LocalRunner`
as their only dependency so the caller controls the runner lifetime.
"""

from __future__ import annotations

from datetime import UTC, datetime

from unlimited_mcp.jobs.result import ErrorBlock, JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner


def get_job_result(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Return the current result for *job_id*, or a ``JOB_NOT_FOUND`` error.

    Zombie detection is applied automatically for jobs still marked
    ``status="running"`` whose process has already exited.
    """
    result = runner.get_result(job_id)
    if result is not None:
        return result
    now = datetime.now(UTC)
    return JobResult(
        ok=False,
        job_id=job_id,
        status="failed",
        tool="get_job_result",
        started_at=now,
        finished_at=now,
        summary=f"Job {job_id!r} not found.",
        error=ErrorBlock(
            code="JOB_NOT_FOUND",
            message=f"No job with id {job_id!r}.",
            hint="Call list_jobs() to see available job IDs.",
        ),
    )


def list_jobs(*, runner: LocalRunner) -> list[JobResult]:
    """Return all known :class:`~unlimited_mcp.jobs.result.JobResult` objects.

    Results are ordered by job_id (natural timestamp order). Zombie detection
    is applied to any job still in ``status="running"``.
    """
    return runner.list_results()


def cancel_job(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Send ``SIGTERM`` to *job_id* and mark it cancelled.

    If the job is already finished (completed/failed/cancelled) the existing
    result is returned unchanged. If the job is unknown a ``JOB_NOT_FOUND``
    error result is returned.
    """
    return runner.cancel(job_id)
