"""MCP tool functions: run_command, run_shell, delegate_to_agent, run_and_summarize.

These are pure Python functions — no MCP SDK import, no global state.
The server wires them up as tools in a later phase; dependencies are injected
by the caller.

Safety contract
---------------
* ``SafetyChecker.check_run_command`` returns, never raises.
* Hard blocks  → ``status="failed"``  + populated ``error`` block.
* Soft blocks  → ``status="pending_confirmation"`` + ``confirm_token``.
* ``AgentRenderError`` from ``AgentRunner.submit`` propagates as-is.
* ``ProviderError`` from ``provider.complete`` is caught; summarisation is
  best-effort — the original result is returned unchanged on failure.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unlimited_mcp.jobs.result import ErrorBlock, JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.providers.base import Provider, ProviderError
from unlimited_mcp.safety.argv_check import SafetyChecker, SafetyDecision

if TYPE_CHECKING:
    from unlimited_mcp.agents.runner import AgentRunner

_POLL_INTERVAL = 0.25


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def run_command(
    argv: list[str],
    *,
    safety: SafetyChecker,
    runner: LocalRunner,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    timeout_seconds: int = 600,
    confirm_token: str | None = None,
    tool: str = "run_command",
) -> JobResult:
    """Apply the safety pipeline to *argv* then submit to :class:`LocalRunner`."""
    decision = safety.check_run_command(argv, cwd=cwd, confirm_token=confirm_token)
    if not decision.allowed:
        return _blocked_result(decision, tool=tool)
    return runner.submit(
        argv,
        cwd=cwd,
        env_extra=env_extra,
        timeout_seconds=timeout_seconds,
        tool=tool,
    )


def delegate_to_agent(
    agent_name: str,
    *,
    agent_runner: AgentRunner,
    prompt: str | None = None,
    files: list[str] | None = None,
    params_override: dict[str, Any] | None = None,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    timeout_seconds: int = 600,
    idempotency_key: str | None = None,
    confirm_token: str | None = None,
    workspace_override: str | None = None,
    tag: str | None = None,
    runner_override: Any | None = None,
    clarify_rounds: int = 0,
) -> JobResult:
    """Thin wrapper around :meth:`AgentRunner.submit`."""
    return agent_runner.submit(
        agent_name,
        prompt=prompt,
        files=files,
        params_override=params_override,
        cwd=cwd,
        env_extra=env_extra,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
        confirm_token=confirm_token,
        workspace_override=workspace_override,
        tag=tag,
        runner_override=runner_override,
        clarify_rounds=clarify_rounds,
    )


def run_shell(
    script: str,
    *,
    safety: SafetyChecker,
    runner: LocalRunner,
    interpreter: str = "bash",
    i_understand_this_runs_a_shell_script: bool = False,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    timeout_seconds: int = 60,
    tool: str = "run_shell",
) -> JobResult:
    """Run *script* via *interpreter* after the safety pipeline.

    Unlike ``run_command``, the script is passed verbatim to the shell, so
    pipes, redirections, loops, and expansions all work.  The trade-off is
    that static argv classification is impossible — the job is always at
    least ``mutating``.

    ``i_understand_this_runs_a_shell_script`` must be ``True``; it exists so
    callers cannot trigger shell execution accidentally.
    """
    if not i_understand_this_runs_a_shell_script:
        now = datetime.now(UTC)
        return JobResult(
            ok=False,
            job_id=JobStore.make_job_id(tool),
            status="failed",
            tool=tool,
            started_at=now,
            finished_at=now,
            risk_level="medium",
            error=ErrorBlock(
                code="SHELL_SCRIPT_OPT_IN_REQUIRED",
                message="i_understand_this_runs_a_shell_script must be True.",
                hint=(
                    "Set i_understand_this_runs_a_shell_script=True to confirm you "
                    "intend to run an arbitrary shell script. Unlike run_command, "
                    "the script bypasses static safety classification."
                ),
            ),
            summary="Opt-in flag not set; call was not executed.",
        )

    decision = safety.check_run_shell(cwd=cwd)
    if not decision.allowed:
        return _blocked_result(decision, tool=tool)

    argv = [interpreter, "-c", script]
    return runner.submit(
        argv,
        cwd=cwd,
        env_extra=env_extra,
        timeout_seconds=timeout_seconds,
        tool=tool,
    )


def run_and_summarize(
    argv: list[str],
    *,
    safety: SafetyChecker,
    runner: LocalRunner,
    provider: Provider | None = None,
    model: str | None = None,
    cwd: str | None = None,
    timeout_seconds: int = 600,
    confirm_token: str | None = None,
    _poll_interval: float = _POLL_INTERVAL,
) -> JobResult:
    """Submit *argv*, poll until completion, then optionally summarise output.

    Parameters
    ----------
    provider:
        When ``None`` the summarisation step is skipped entirely.
    _poll_interval:
        Seconds between ``get_result`` polls.  Exposed for tests.
    """
    result = run_command(
        argv,
        safety=safety,
        runner=runner,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        confirm_token=confirm_token,
        tool="run_and_summarize",
    )
    if result.status in ("failed", "pending_confirmation"):
        return result

    while result.status in ("running", "queued"):
        time.sleep(_poll_interval)
        polled = runner.get_result(result.job_id)
        if polled is None:
            break
        result = polled

    if provider is None or result.status != "completed":
        return result

    stdout_text = _read_stdout(result)
    messages: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                f"Summarize the output of: {' '.join(argv)}\n\nstdout:\n{stdout_text or '(empty)'}"
            ),
        }
    ]
    try:
        summary = provider.complete(messages, model=model)
    except ProviderError:
        return result

    return result.model_copy(update={"summary": summary})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _blocked_result(decision: SafetyDecision, *, tool: str) -> JobResult:
    now = datetime.now(UTC)
    job_id = JobStore.make_job_id(tool)
    if decision.requires_confirmation:
        return JobResult(
            ok=False,
            job_id=job_id,
            status="pending_confirmation",
            tool=tool,
            started_at=now,
            finished_at=now,
            risk_level=decision.risk_level,
            blast_radius=decision.blast_radius,
            confirm_token=decision.confirm_token,
            confirm_reason=decision.confirm_reason,
            summary=decision.confirm_reason,
        )
    return JobResult(
        ok=False,
        job_id=job_id,
        status="failed",
        tool=tool,
        started_at=now,
        finished_at=now,
        risk_level=decision.risk_level,
        blast_radius=decision.blast_radius,
        error=ErrorBlock(
            code=decision.error_code or "SAFETY_BLOCKED",
            message=decision.error_hint or "Blocked by safety pipeline.",
            hint=decision.error_hint,
        ),
        summary=decision.error_hint or "Blocked by safety pipeline.",
    )


def _read_stdout(result: JobResult, max_bytes: int = 8192) -> str:
    if not result.raw_output_ref:
        return ""
    path = Path(result.raw_output_ref)
    if not path.exists():
        return ""
    raw = path.read_bytes()[-max_bytes:]
    return raw.decode("utf-8", errors="replace")
