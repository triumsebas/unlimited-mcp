"""AgentRunner: glue between :class:`CLIAgent`, the safety pipeline,
:class:`~unlimited_mcp.workspace.manager.WorkspaceManager`, and
:class:`~unlimited_mcp.jobs.runner_local.LocalRunner`.

The runner has a single responsibility: take an *agent name* + invocation
parameters, resolve them into a final argv via :class:`CLIAgent`, create
the appropriate workspace (worktree, temp copy, or current dir) per the
agent's preset, run the safety pipeline, and dispatch allowed jobs to
:class:`LocalRunner`. Every code path returns a
:class:`~unlimited_mcp.jobs.result.JobResult`:

* allowed                       → the running JobResult from LocalRunner.
* requires_confirmation         → ``status="pending_confirmation"`` with
  ``confirm_token`` and ``confirm_reason`` populated.
* error_code (hard block)       → ``status="failed"`` with a populated
  ``error`` block.

Workspace lifecycle
-------------------
For ``git_worktree`` mode (``safe_dev`` preset):

1. :class:`~unlimited_mcp.workspace.manager.WorkspaceManager` creates a
   fresh branch ``unlimited-mcp/<label>-<ts>-<hex>`` and a worktree at
   ``<base_dir>/<label>-<ts>-<hex>``.
2. The agent runs inside that worktree (``cwd = workspace.path``).
3. After the watcher thread records the final result, it calls
   ``workspace.cleanup()`` which removes the worktree directory. The
   branch is kept (``leave_branch`` result) so the orchestrator can
   review or merge it.
4. :attr:`JobResult.branch` and :attr:`JobResult.worktree_path` carry
   the branch name and worktree path so the orchestrator knows where to
   look.

For ``current`` / ``sysops_local`` / ``none`` modes the workspace is a
no-op and ``branch``/``worktree_path`` are ``None``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unlimited_mcp.agents.base import CLIAgent
from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import Config, Knowledge
from unlimited_mcp.jobs.result import ErrorBlock, JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.argv_check import SafetyChecker, SafetyDecision
from unlimited_mcp.workspace.manager import WorkspaceManager

log = logging.getLogger(__name__)

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
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self._config = config
        self._knowledge = knowledge
        self._local_runner = local_runner
        self._safety = safety
        self._workspace_manager = workspace_manager

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
        workspace_override: str | None = None,
        tool: str = DEFAULT_TOOL_NAME,
    ) -> JobResult:
        """Render the agent's argv, prepare workspace, apply safety, and submit.

        :class:`~unlimited_mcp.agents.base.AgentRenderError` from
        :meth:`CLIAgent.from_config` / :meth:`CLIAgent.render_argv` is allowed
        to propagate — those are programmer/config errors, distinct from the
        runtime safety pipeline.
        """
        cfg = self._get_config()
        kn = self._get_knowledge()

        agent = CLIAgent.from_config(agent_name, cfg, kn)

        # ---- workspace -------------------------------------------------------
        workspace = None
        effective_cwd = cwd
        branch: str | None = None
        worktree_path: str | None = None

        if self._workspace_manager is not None:
            agent_cfg = cfg.agents.get(agent_name)
            # workspace_override="" or "none" explicitly disables worktree.
            if workspace_override is not None:
                workspace_preset = workspace_override if workspace_override not in ("", "none") else None
            else:
                workspace_preset = agent_cfg.workspace if agent_cfg else None
            if workspace_preset and cwd is not None:
                try:
                    workspace = self._workspace_manager.create(
                        workspace_preset,
                        source=Path(cwd),
                        label=agent_name.replace("_", "-"),
                    )
                    effective_cwd = str(workspace.path)
                    branch = workspace.branch
                    worktree_path = str(workspace.path) if workspace.branch else None
                except Exception as exc:
                    log.warning("Workspace creation failed for %r: %s", agent_name, exc)
                    # Fall back to running in the original cwd without a worktree.
                    workspace = None

        # ---- render argv (uses effective_cwd for {cwd} token) ----------------
        argv = agent.render_argv(
            prompt=prompt,
            files=files,
            params_override=params_override,
            cwd=effective_cwd,
        )

        # ---- safety ----------------------------------------------------------
        decision = self._safety.check_run_command(
            argv, cwd=effective_cwd, confirm_token=confirm_token
        )
        if not decision.allowed:
            if workspace is not None:
                try:
                    workspace.cleanup()
                except Exception:
                    pass
            return _decision_to_blocked_result(decision, agent_name=agent_name, tool=tool)

        # ---- submit ----------------------------------------------------------
        cleanup_fn = workspace.cleanup if workspace is not None else None
        return self._local_runner.submit(
            argv,
            label=agent_name,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            env_extra=env_extra,
            cwd=effective_cwd,
            tool=tool,
            branch=branch,
            worktree_path=worktree_path,
            cleanup_fn=cleanup_fn,
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
