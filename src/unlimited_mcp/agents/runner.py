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
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unlimited_mcp.agents.base import CLIAgent
from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import Config, Knowledge
from unlimited_mcp.jobs.result import ErrorBlock, JobResult, JobWarning
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.argv_check import SafetyChecker, SafetyDecision
from unlimited_mcp.workspace.manager import WorkspaceManager

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from unlimited_mcp.hosts.registry import HostRegistry
    from unlimited_mcp.jobs.runner_remote import RemoteRunner

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
        host_registry: HostRegistry | None = None,
    ) -> None:
        self._config = config
        self._knowledge = knowledge
        self._local_runner = local_runner
        self._safety = safety
        self._workspace_manager = workspace_manager
        self._host_registry = host_registry

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
        exec_host_override: str | None = None,
        tag: str | None = None,
        runner_override: Any | None = None,
        tool: str = DEFAULT_TOOL_NAME,
        clarify_rounds: int = 0,
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

        # ---- env_extra: merge agent-level defaults with call-time overrides --
        agent_cfg = cfg.agents.get(agent_name)
        merged_env: dict[str, str] = {}
        if agent_cfg and agent_cfg.env_extra:
            for k, v in agent_cfg.env_extra.items():
                merged_env[k] = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v)
        if env_extra:
            merged_env.update(env_extra)
        env_extra = merged_env or None

        # ---- exec_host resolution --------------------------------------------
        exec_host = exec_host_override or (agent_cfg.exec_host if agent_cfg else "local")
        is_remote = exec_host != "local"

        # ---- workspace -------------------------------------------------------
        # Skip workspace management for remote agents: the worktree would need
        # to exist on the remote machine.  Use workspace="none" in that case.
        workspace = None
        effective_cwd = cwd
        branch: str | None = None
        worktree_path: str | None = None

        if not is_remote and self._workspace_manager is not None:
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

        # ---- clarify preamble ------------------------------------------------
        warnings: list[JobWarning] = []
        pre_job_id: str | None = None
        _remote_q_dir: str | None = None
        _original_prompt: str = prompt or ""

        if clarify_rounds > 0:
            # Detect runner type for Q&A routing.
            _is_remote_ts = False
            try:
                from unlimited_mcp.jobs.runner_remote_ts import RemoteTsRunner as _RemoteTsRunner
                _is_remote_ts = isinstance(runner_override, _RemoteTsRunner)
            except ImportError:
                pass

            agent_cfg = cfg.agents.get(agent_name)
            if agent_cfg is not None and not agent_cfg.supports_clarify:
                warnings.append(JobWarning(
                    code="CLARIFY_NOT_SUPPORTED",
                    message=f"Agent {agent_name!r} has supports_clarify=False; Q&A phase skipped.",
                    hint="Set supports_clarify=true in config.yaml if the agent has been verified.",
                ))
                clarify_rounds = 0
            else:
                clarify_cfg = cfg.clarify
                effective_rounds = min(clarify_rounds, clarify_cfg.max_rounds)
                pre_job_id = JobStore.make_job_id(tool)
                active_runner = runner_override if runner_override is not None else self._local_runner
                active_runner._store.create(pre_job_id)
                q_dir = active_runner._store.questions_dir(pre_job_id)
                if _is_remote_ts or is_remote:
                    _remote_q_dir = f"/tmp/umcp-{pre_job_id}/questions"
                    clarify_prompt_dir: str | Path = _remote_q_dir
                else:
                    clarify_prompt_dir = q_dir
                prompt = _build_clarify_prompt(
                    original_prompt=prompt or "",
                    questions_dir=clarify_prompt_dir,
                    max_rounds=effective_rounds,
                    max_total_seconds=clarify_cfg.max_total_seconds,
                )
                # The clarify phase (repo scan + writing questions + waiting
                # for answers) runs *before* any real work and would otherwise
                # eat into the job's work budget. Extend the timeout so the
                # answer-wait window is additive, not subtractive.
                timeout_seconds += clarify_cfg.max_total_seconds

        # ---- render argv (uses effective_cwd for {cwd} token) ----------------
        render = agent.render_argv(
            prompt=prompt,
            files=files,
            params_override=params_override,
            cwd=effective_cwd,
        )

        # ---- safety ----------------------------------------------------------
        decision = self._safety.check_run_command(
            render.argv, cwd=effective_cwd, confirm_token=confirm_token
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

        # runner_override (queue selection) takes precedence; then exec_host.
        if runner_override is not None:
            active_runner = runner_override
        elif is_remote:
            if self._host_registry is None:
                raise RuntimeError(
                    f"Agent {agent_name!r} has exec_host={exec_host!r} but no "
                    "HostRegistry is configured in AgentRunner."
                )
            from unlimited_mcp.jobs.runner_remote import RemoteRunner
            host = self._host_registry.get(exec_host)
            active_runner = RemoteRunner(host, self._local_runner._store)
        else:
            active_runner = self._local_runner
        extra_kw: dict[str, Any] = {}
        if _remote_q_dir is not None:
            extra_kw["remote_questions_dir"] = _remote_q_dir
        result = active_runner.submit(
            render.argv,
            label=agent_name,
            tag=tag,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            env_extra=env_extra,
            cwd=effective_cwd,
            tool=tool,
            branch=branch,
            worktree_path=worktree_path,
            cleanup_fn=cleanup_fn,
            stdin_content=render.stdin_content,
            prompt_file_content=render.prompt_file_content,
            job_id=pre_job_id,
            **extra_kw,
        )
        # Patch original_prompt into meta so resume_agent_task can reconstruct
        # the pre-clarify prompt even when the job ran on a remote host.
        if _original_prompt:
            meta = self._local_runner._store.read_meta(result.job_id) or {}
            meta["original_prompt"] = _original_prompt
            self._local_runner._store.write_meta(result.job_id, meta)
        if warnings:
            result = result.model_copy(update={"warnings": result.warnings + warnings})
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_clarify_prompt(
    original_prompt: str,
    questions_dir: str | Path,
    max_rounds: int,
    max_total_seconds: int,
) -> str:
    """Prepend the file-based Q&A clarification protocol to the original prompt.

    The agent writes all questions for a round to a single JSON array file,
    waits for an answers file, then proceeds.  If the total wait exceeds
    *max_total_seconds* the agent must write a timeout marker and exit 2.
    """
    poll_interval = 3
    return f"""## Pre-task clarification protocol

This is the VERY FIRST thing you do — before reading more than the minimum
code needed to know what to ask, before any planning, before any edits.
Resolve ambiguity through files, not assumptions.

**Rules:**
- Write ALL your questions for a round at once in a single file — do not ask one at a time.
- Decide what to ask quickly. Do not deep-dive the whole repo before round 001;
  a short scan to identify the real ambiguities is enough.
- If you have NO questions (the task is unambiguous), you MUST still write
  `round_001_questions.json` with an empty array `[]` and then proceed
  immediately. This is the signal that you will not ask — never skip it.
- Only use a second round if the first answers revealed something genuinely unexpected.
- Do not assume. Do not invent answers.
- If you receive an answer containing "STOP", proceed immediately with what you know.
- Only ask what would actually change your implementation.
- Maximum {max_rounds} rounds. Total wait budget: {max_total_seconds}s.

**Protocol for round N (format N as a zero-padded 3-digit number: 001, 002, ...):**

Step 1 — write your questions to:
  {questions_dir}/round_NNN_questions.json
  Format: [{{"id": 1, "question": "...", "options": ["A: ...", "B: ..."], "why": "one line on why this changes the implementation"}}, ...]
  If you have no questions, write exactly: []
  When the file is `[]`, skip steps 2-3 and start the task now.

Step 2 — poll every {poll_interval}s for answers at:
  {questions_dir}/round_NNN_answers.json
  Print "round N: waiting for answers..." each iteration.
  If the file does not appear within your remaining time budget, write:
  {questions_dir}/timeout.json  ← {{"last_round": N, "unanswered_questions": [...]}}
  Then exit with code 2.

Step 3 — read the answers. If you need a follow-up round (and rounds remain), write round N+1. Otherwise proceed with the task.

---

## Your task

{original_prompt}"""


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
