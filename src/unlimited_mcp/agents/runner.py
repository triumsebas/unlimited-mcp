"""AgentRunner: glue between :class:`CLIAgent`, the safety pipeline, and
:class:`~unlimited_mcp.jobs.runner_local.LocalRunner`.

The runner has a single responsibility: take an *agent name* + invocation
parameters, resolve them into a final argv via :class:`CLIAgent`, run the
safety pipeline, and dispatch allowed jobs to :class:`LocalRunner`. Every
code path returns a :class:`~unlimited_mcp.jobs.result.JobResult`:

* allowed                       → the running JobResult from LocalRunner.
* requires_confirmation         → ``status="pending_confirmation"`` with
  ``confirm_token`` and ``confirm_reason`` populated.
* error_code (hard block)       → ``status="failed"`` with a populated
  ``error`` block.

What the runner deliberately does *not* own
-------------------------------------------
* **Workspace lifecycle.** The caller passes ``cwd: str | None``; cloning
  a repo, creating a worktree, and cleaning it up are the tool layer's
  job because they outlive a single async ``submit`` call.
* **Provider summarisation.** ``CLIAgent`` and :class:`Provider` are
  orthogonal abstractions; ``run_and_summarize`` composes them at the
  tool layer instead of inside the runner.

The runner is synchronous: ``submit`` returns immediately with a
``status="running"`` JobResult (or a blocked one).  The MCP tool layer
polls :meth:`LocalRunner.get_result` to deliver the final outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from unlimited_mcp.agents.base import CLIAgent
from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import Config, Knowledge
from unlimited_mcp.jobs.result import ErrorBlock, JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.argv_check import SafetyChecker, SafetyDecision

DEFAULT_TOOL_NAME = "delegate_to_agent"


class AgentRunner:
    """Resolve an agent name into an argv, apply safety, and submit."""

    def __init__(
        self,
        *,
        config: Config | ConfigStore,
        knowledge: Knowledge | KnowledgeStore,
        local_runner: LocalRunner,
        safety: SafetyChecker,
    ) -> None:
        self._config = config
        self._knowledge = knowledge
        self._local_runner = local_runner
        self._safety = safety

    def _get_config(self) -> Config:
        return self._config.get() if isinstance(self._config, ConfigStore) else self._config

    def _get_knowledge(self) -> Knowledge:
        return self._knowledge.get() if isinstance(self._knowledge, KnowledgeStore) else self._knowledge

    def submit(
        self,
        agent_name: str,
        *,
        prompt: str | None = None,
        files: list[str] | None = None,
        params_override: dict[str, Any] | None = None,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        idempotency_key: str | None = None,
        confirm_token: str | None = None,
        tool: str = DEFAULT_TOOL_NAME,
    ) -> JobResult:
        """Render the agent's argv, apply safety, and submit if allowed.

        :class:`~unlimited_mcp.agents.base.AgentRenderError` from
        :meth:`CLIAgent.from_config` / :meth:`CLIAgent.render_argv` is allowed
        to propagate — those are programmer/config errors, distinct from the
        runtime safety pipeline.
        """
        agent = CLIAgent.from_config(agent_name, self._get_config(), self._get_knowledge())
        argv = agent.render_argv(
            prompt=prompt,
            files=files,
            params_override=params_override,
            cwd=cwd,
        )
        decision = self._safety.check_run_command(argv, cwd=cwd, confirm_token=confirm_token)
        if not decision.allowed:
            return _decision_to_blocked_result(decision, agent_name=agent_name, tool=tool)
        return self._local_runner.submit(
            argv,
            label=agent_name,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            env_extra=env_extra,
            cwd=cwd,
            tool=tool,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decision_to_blocked_result(
    decision: SafetyDecision,
    *,
    agent_name: str,
    tool: str,
) -> JobResult:
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
    error = ErrorBlock(
        code=decision.error_code or "SAFETY_BLOCKED",
        message=decision.error_hint or "Invocation blocked by the safety pipeline.",
        hint=decision.error_hint,
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
        summary=f"Agent {agent_name!r} blocked: {error.message}",
        error=error,
    )
