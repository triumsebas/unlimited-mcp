"""Job-management tool functions: submit_task, get_job_status, get_job_result, list_jobs, cancel_job.

These are pure Python functions — no MCP SDK import, no global state.
The server registers them as MCP tools in a later step.

All functions accept a :class:`~unlimited_mcp.jobs.runner_local.LocalRunner`
as their only dependency so the caller controls the runner lifetime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from unlimited_mcp.jobs.result import ErrorBlock, JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner

if TYPE_CHECKING:
    from unlimited_mcp.agents.runner import AgentRunner
    from unlimited_mcp.safety.argv_check import SafetyChecker


def submit_task(
    *,
    argv: list[str] | None = None,
    agent_name: str | None = None,
    prompt: str | None = None,
    label: str = "",
    timeout_seconds: int = 600,
    idempotency_key: str | None = None,
    cwd: str | None = None,
    env_extra: dict[str, Any] | None = None,
    runner: LocalRunner,
    safety: SafetyChecker,
    agent_runner: AgentRunner,
) -> JobResult:
    """Submit a command or agent invocation as a background job.

    Pass either *argv* (raw command) or *agent_name* + *prompt* (agent
    dispatch).  Returns immediately with ``status="running"``.
    Idempotency: if *idempotency_key* matches a non-failed existing job,
    that job's result is returned without a new submission.
    """
    if argv is None and agent_name is None:
        now = datetime.now(UTC)
        from unlimited_mcp.jobs.store import JobStore

        return JobResult(
            ok=False,
            job_id=JobStore.make_job_id("submit_task"),
            status="failed",
            tool="submit_task",
            started_at=now,
            finished_at=now,
            summary="Either argv or agent_name must be provided.",
            error=ErrorBlock(
                code="TOOL_INPUT_INVALID",
                message="Either argv or agent_name must be provided.",
                hint="Pass argv=[...] for a raw command or agent_name+prompt for an agent.",
            ),
        )

    if argv is not None:
        from unlimited_mcp.tools.execution import run_command

        return run_command(
            argv,
            safety=safety,
            runner=runner,
            cwd=cwd,
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            tool="submit_task",
        )

    return agent_runner.submit(
        agent_name,  # type: ignore[arg-type]
        prompt=prompt,
        cwd=cwd,
        env_extra=env_extra,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
    )


def get_job_status(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Return the current status of *job_id* (lightweight poll, same shape as get_job_result)."""
    return get_job_result(job_id, runner=runner)


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
