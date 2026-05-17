"""MCP server factory for unlimited-mcp.

Wires all tool functions into a :class:`~mcp.server.fastmcp.FastMCP` instance
ready to be served over stdio.

Lifetime model
--------------
All stateful objects (``LocalRunner``, ``SafetyChecker``, ``AgentRunner``,
``ConfigStore``, ``KnowledgeStore``) are created once inside
:func:`make_server` and shared across all tool calls via closure.

Usage::

    from unlimited_mcp.server import make_server
    app = make_server()
    app.run()                  # blocks, stdio transport
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from unlimited_mcp.agents.runner import AgentRunner
from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.hosts.registry import HostRegistry
from unlimited_mcp.jobs.result import JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.runner_remote import RemoteRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.paths import (
    config_path,
    jobs_dir,
    knowledge_local_path,
    state_dir,
)
from unlimited_mcp.workspace.manager import WorkspaceManager
from unlimited_mcp.providers.base import Provider
from unlimited_mcp.providers.openai_compat import OpenAICompatProvider
from unlimited_mcp.config.schema import ProviderConfig
from unlimited_mcp.safety.argv_check import SafetyChecker
from unlimited_mcp.tools.config_tools import (
    add_agent as _add_agent,
    add_allowed_root as _add_allowed_root,
    add_deny_path as _add_deny_path,
    add_host as _add_host,
    add_provider as _add_provider,
    add_queue as _add_queue,
    configure_agent as _configure_agent,
    configure_safety as _configure_safety,
    list_capabilities as _list_capabilities,
    list_safety_policy as _list_safety_policy,
    remove_allowed_root as _remove_allowed_root,
    remove_deny_path as _remove_deny_path,
    remove_entry as _remove_entry,
    ssh_trust_host as _ssh_trust_host,
)
from unlimited_mcp.tools.execution import delegate_to_agent as _delegate_to_agent
from unlimited_mcp.tools.execution import run_and_summarize as _run_and_summarize
from unlimited_mcp.tools.execution import run_command as _run_command
from unlimited_mcp.tools.execution import run_shell as _run_shell
from unlimited_mcp.observability.log_query import query_logs as _query_logs
from unlimited_mcp.tools.jobs import cancel_job as _cancel_job
from unlimited_mcp.tools.jobs import cleanup_branches as _cleanup_branches
from unlimited_mcp.tools.jobs import cleanup_jobs as _cleanup_jobs
from unlimited_mcp.tools.jobs import await_job as _await_job
from unlimited_mcp.tools.jobs import get_job_result as _get_job_result
from unlimited_mcp.tools.jobs import get_job_status as _get_job_status
from unlimited_mcp.tools.jobs import list_jobs as _list_jobs
from unlimited_mcp.tools.jobs import submit_task as _submit_task
from unlimited_mcp.tools.knowledge_tools import (
    lookup_agent_cli as _lookup_agent_cli,
    register_agent_knowledge as _register_agent_knowledge,
)
from unlimited_mcp.tools.workers_tools import (
    answer_worker_questions as _answer_worker_questions,
    get_worker_questions as _get_worker_questions,
    resume_agent_task as _resume_agent_task,
)

_REPO_KNOWLEDGE_PATH = Path(__file__).parent / "knowledge.yaml"


_OPENAI_COMPAT_DEFAULT_URLS: dict[str, str] = {
    "ollama":     "http://localhost:11434/v1",
    "mlx_lm":     "http://localhost:8080/v1",
    "gemini":     "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq":       "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

_OPENAI_COMPAT_TYPES = frozenset(
    ["openai_compat", "ollama", "mlx_lm", "gemini", "groq", "openrouter"]
)


def _build_provider(name: str, cfg: ProviderConfig) -> Provider | None:
    """Instantiate a Provider from a ProviderConfig entry."""
    if cfg.type in _OPENAI_COMPAT_TYPES:
        api_key = (
            cfg.api_key
            or (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "")
        )
        base_url = (
            os.environ.get(cfg.api_base_env, cfg.base_url or "")
            if cfg.api_base_env
            else (cfg.base_url or _OPENAI_COMPAT_DEFAULT_URLS.get(cfg.type, ""))
        )
        if not base_url:
            return None
        return OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            default_model=cfg.model,
            name=name,
        )
    return None


def make_server(
    config_file: Path | None = None,
    *,
    knowledge_repo: Path | None = None,
    knowledge_local: Path | None = None,
    jobs_path: Path | None = None,
    provider: Provider | None = None,
    provider_model: str | None = None,
    server_name: str = "unlimited-mcp",
) -> FastMCP:
    """Instantiate all dependencies and return a wired :class:`FastMCP`.

    Parameters
    ----------
    config_file:
        Path to ``config.yaml``.  Defaults to :func:`~unlimited_mcp.paths.config_path`.
    knowledge_repo:
        Path to the shared ``knowledge.yaml`` in the repo root.
    knowledge_local:
        Path to ``knowledge.local.yaml`` in the user config dir.
    jobs_path:
        Root directory for job state files.
    provider:
        Optional LLM provider used by ``run_and_summarize`` for summarisation.
    provider_model:
        Model override forwarded to ``provider.complete``.
    server_name:
        Human-readable name passed to :class:`FastMCP`.
    """
    kl_path = knowledge_local or knowledge_local_path()
    cfg_file = config_file or config_path()

    cfg_store = ConfigStore(cfg_file)
    kn_store = KnowledgeStore(
        knowledge_repo or _REPO_KNOWLEDGE_PATH,
        kl_path,
    )
    job_store = JobStore(jobs_path or jobs_dir())
    runner = LocalRunner(job_store)
    host_registry = HostRegistry(cfg_store)
    _remote_runners: dict[str, RemoteRunner] = {}

    # Build TsRunner instances from the queues section of config.
    # Defaults are injected for the two built-in queues (ts / ts_serial) so the
    # server works out of the box even without an explicit queues: block.
    _cfg_snapshot = cfg_store.get()
    _queue_defaults = {
        "ts":        {"type": "local_ts", "slots": 4, "socket": None, "host": None},
        "ts_serial": {"type": "local_ts", "slots": 1, "socket": None, "host": None},
    }
    _queue_cfgs: dict[str, object] = {}
    for _qname, _qdef in _queue_defaults.items():
        _qcfg = _cfg_snapshot.queues.get(_qname)
        _queue_cfgs[_qname] = {
            "type":   _qcfg.type   if _qcfg else _qdef["type"],
            "slots":  _qcfg.slots  if _qcfg else _qdef["slots"],
            "socket": _qcfg.socket if _qcfg else _qdef["socket"],
            "host":   _qcfg.host   if _qcfg else _qdef["host"],
        }
    # Also pick up any extra named queues defined in config.
    for _qname, _qcfg in _cfg_snapshot.queues.items():
        if _qname not in _queue_cfgs:
            _queue_cfgs[_qname] = {
                "type":   _qcfg.type,
                "slots":  _qcfg.slots,
                "socket": _qcfg.socket,
                "host":   _qcfg.host,
            }

    _ts_runners: dict[str, object] = {}
    try:
        from unlimited_mcp.jobs.runner_ts import TsRunner
        from unlimited_mcp.jobs.runner_remote_ts import RemoteTsRunner as _RemoteTsRunner
        import logging as _lg
        _ql = _lg.getLogger(__name__)
        for _qname, _qopt in _queue_cfgs.items():
            _qtype = _qopt["type"]  # type: ignore[index]
            if _qtype == "local_ts":
                _sock = Path(_qopt["socket"]) if _qopt["socket"] else state_dir() / f"{_qname}.sock"  # type: ignore[index]
                _ts_runners[_qname] = TsRunner(job_store, ts_socket=_sock, max_slots=int(_qopt["slots"]))  # type: ignore[index]
            elif _qtype == "remote_ts":
                _host_name = _qopt.get("host")  # type: ignore[union-attr]
                if not _host_name:
                    _ql.warning("Queue %r: remote_ts requires a 'host' field — skipping.", _qname)
                    continue
                _host_cfg = _cfg_snapshot.hosts.get(_host_name)
                if _host_cfg is None or _host_cfg.type != "ssh":
                    _ql.warning(
                        "Queue %r: host %r must be type 'ssh' for remote_ts — skipping.",
                        _qname, _host_name,
                    )
                    continue
                try:
                    _ssh_host = host_registry.get(_host_name)
                except KeyError:
                    _ql.warning("Queue %r: host %r not found in config — skipping.", _qname, _host_name)
                    continue
                _ts_runners[_qname] = _RemoteTsRunner(
                    _ssh_host,  # type: ignore[arg-type]
                    job_store,
                    ts_socket=_qopt.get("socket"),  # type: ignore[union-attr]
                    max_slots=int(_qopt["slots"]),  # type: ignore[index]
                )
            else:
                _ql.warning("Queue %r has unsupported type %r — skipping.", _qname, _qtype)
    except Exception as _exc:
        import logging as _lg
        _lg.getLogger(__name__).warning("Runner init error: %s", _exc)

    ts_runner = _ts_runners.get("ts")
    ts_serial_runner = _ts_runners.get("ts_serial")
    # Pass stores (not snapshots) so config changes made via MCP tools
    # (add_agent, add_allowed_root, …) are visible on the next tool call.
    safety = SafetyChecker(cfg_store, kn_store)
    worktree_base = state_dir() / "work"
    worktree_base.mkdir(parents=True, exist_ok=True)
    workspace_mgr = WorkspaceManager(kn_store.get(), worktree_base)
    agent_runner = AgentRunner(
        config=cfg_store,
        knowledge=kn_store,
        local_runner=runner,
        safety=safety,
        workspace_manager=workspace_mgr,
        host_registry=host_registry,
    )

    def _pick_host_runner(exec_host: str) -> LocalRunner | RemoteRunner:
        """Resolve exec_host name to a runner (cached per host)."""
        if exec_host == "local":
            return runner
        if exec_host not in _remote_runners:
            host = host_registry.get(exec_host)
            _remote_runners[exec_host] = RemoteRunner(host, job_store)
        return _remote_runners[exec_host]

    def _default_cwd(exec_host: str, cwd: str | None) -> str | None:
        """Return cwd, falling back to the host's repos_root when cwd is None."""
        if cwd is not None or exec_host == "local" or host_registry is None:
            return cwd
        try:
            host_cfg = host_registry.get(exec_host)._config  # type: ignore[union-attr]
            return getattr(host_cfg, "repos_root", None) or cwd
        except Exception:
            return cwd

    def _host_extra_roots(exec_host: str) -> list[str] | None:
        """Return the per-host allowed_roots + repos_root to pass as extra_allowed_roots.

        These paths live on the remote machine so they are not in the global
        allowed_roots list, but they must be reachable for remote jobs.
        """
        if exec_host == "local" or host_registry is None:
            return None
        try:
            host_cfg = host_registry.get(exec_host)._config  # type: ignore[union-attr]
            extras: list[str] = list(getattr(host_cfg, "allowed_roots", None) or [])
            repos_root = getattr(host_cfg, "repos_root", None)
            if repos_root and repos_root not in extras:
                extras.append(repos_root)
            return extras or None
        except Exception:
            return None

    app = FastMCP(server_name)

    # ------------------------------------------------------------------
    # Execution tools
    # ------------------------------------------------------------------

    @app.tool()
    def run_command(
        argv: list[str],
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        confirm_token: str | None = None,
        exec_host: str = "local",
    ) -> JobResult:
        """Run a command after the safety pipeline.

        argv must be a list — no shell interpolation. Returns immediately with
        status='running'. Poll with get_job_result(job_id) to check completion.

        Safety blocks return status='failed' (OUT_OF_ROOT, SHELL_LIKE_BLOCKED) or
        status='pending_confirmation' with a confirm_token for dangerous commands.
        Re-call with confirm_token=<token> to proceed after user approval.

        exec_host: name of a host from config.hosts, or 'local' (default).
          When set to an SSH host, the command runs on the remote machine.
          The host must be configured with proper SSH auth (see SSH.md).
          Example: exec_host='gpu_server'
        """
        return _run_command(
            argv,
            safety=safety,
            runner=_pick_host_runner(exec_host),
            cwd=_default_cwd(exec_host, cwd),
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            confirm_token=confirm_token,
            extra_allowed_roots=_host_extra_roots(exec_host),
        )

    @app.tool()
    def run_shell(
        script: str,
        interpreter: str = "bash",
        i_understand_this_runs_a_shell_script: bool = False,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> JobResult:
        """Run an arbitrary shell script via bash or sh.

        Unlike run_command, the script is passed verbatim to the interpreter,
        so pipes, redirections, loops, variable expansions, and multi-step
        logic all work.

        Use run_command when you have a single known command (argv list).
        Use run_shell when you need shell features: pipes (cmd | grep),
        redirections (> file), loops (for f in *.log), or chained steps
        (make && ./run-tests.sh || notify-failure).

        The job is always safety_class='mutating' — static classification
        is not possible for shell scripts. The cwd must still be inside
        allowed_roots.

        i_understand_this_runs_a_shell_script must be set to True; this
        prevents accidental shell execution.
        """
        return _run_shell(
            script,
            safety=safety,
            runner=runner,
            interpreter=interpreter,
            i_understand_this_runs_a_shell_script=i_understand_this_runs_a_shell_script,
            cwd=cwd,
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
        )

    @app.tool()
    def run_and_summarize(
        argv: list[str],
        cwd: str | None = None,
        timeout_seconds: int = 600,
        confirm_token: str | None = None,
        provider_name: str | None = None,
    ) -> JobResult:
        """Run a command, wait for completion, then summarise stdout via the provider.

        Blocks (polls internally) until the job finishes. Useful for short
        commands where you want the output digested into summary rather than
        reading raw output. If no provider is configured, returns the raw result.

        provider_name: name of a configured provider to use for summarisation.
            When omitted, the first configured provider is used automatically.
        """
        resolved_provider: Provider | None = provider
        if resolved_provider is None:
            cfg_snapshot = cfg_store.get()
            if provider_name:
                pcfg = cfg_snapshot.providers.get(provider_name)
                if pcfg:
                    resolved_provider = _build_provider(provider_name, pcfg)
            elif cfg_snapshot.providers:
                first_name, first_cfg = next(iter(cfg_snapshot.providers.items()))
                resolved_provider = _build_provider(first_name, first_cfg)
        return _run_and_summarize(
            argv,
            safety=safety,
            runner=runner,
            provider=resolved_provider,
            model=provider_model,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            confirm_token=confirm_token,
        )

    def _pick_runner(queue: str) -> object:
        """Return the runner for the requested queue.

        'local'          → in-process LocalRunner (default).
        'ts'             → TsRunner parallel queue (slots configurable via config.yaml).
        'ts_serial'      → TsRunner serial queue (1 slot).
        '<custom_name>'  → any queue defined in the queues: section of config.yaml,
                           including remote_ts queues backed by RemoteTsRunner.

        Falls back to LocalRunner with a warning if the requested queue is
        unavailable (ts not installed, unsupported type, etc.).
        """
        if queue != "local":
            ts_r = _ts_runners.get(queue)
            if ts_r is not None:
                return ts_r  # type: ignore[return-value]
            import logging as _log
            _log.getLogger(__name__).warning(
                "Queue %r unavailable — falling back to local runner. "
                "Check task-spooler is installed: brew install task-spooler", queue,
            )
        return runner

    @app.tool()
    def delegate_to_agent(
        agent_name: str,
        prompt: str | None = None,
        files: list[str] | None = None,
        params_override: dict[str, Any] | None = None,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        idempotency_key: str | None = None,
        confirm_token: str | None = None,
        workspace: str | None = None,
        tag: str | None = None,
        queue: str = "local",
        exec_host: str | None = None,
        clarify_rounds: int = 0,
    ) -> JobResult:
        """Delegate a coding task to a configured agent (aider, opencode, smolagents, ...).

        agent_name must match a key in config.agents. The agent runs in the
        background; poll with get_job_result(job_id).

        workspace overrides the agent's default preset for this call:
          - omit / null → use the agent's configured preset (e.g. 'safe_dev')
          - 'safe_dev'  → git worktree + leave_branch (isolated coding task)
          - 'none' or '' → no workspace management (scripts, analysis, non-repo tasks)
          - 'read_only' → current dir, report only (audits)

        queue: which runner to use for LOCAL execution:
          - 'local' (default) → in-process runner, lighter, no extra deps.
          - 'ts'              → task-spooler, survives MCP restarts.
          Ignored when exec_host is set to a remote host.

        exec_host: run the agent process on a remote machine instead of locally.
          Must match a key in config.hosts. When set, queue is ignored and
          workspace management is skipped (the repo must already exist on the
          remote). Overrides the agent's configured exec_host field.
          Example: exec_host='gpu_server'

        tag: opaque label stored on the JobResult. Use list_jobs(tag=...) to
        recover all jobs from a given session after context loss.

        clarify_rounds: number of Q&A rounds the agent may run before starting
          work (0 = none, default). Only set this for design/planning tasks or
          long tasks where wrong assumptions are costly.

        Example (local):  delegate_to_agent('aider_local',
                            prompt='add docstrings', cwd='/path/to/repo')
        Example (remote): delegate_to_agent('aider_local',
                            prompt='train model', cwd='/home/ubuntu/repo',
                            exec_host='gpu_server')
        """
        # exec_host wins over queue; resolve runner accordingly.
        # When neither an explicit exec_host nor a non-default queue is given,
        # leave runner_override None so AgentRunner honours the agent's own
        # exec_host field (a forced "local" runner here would shadow it).
        resolved_host = exec_host  # may be None → AgentRunner reads agent_cfg.exec_host
        runner_override = (
            None
            if (resolved_host or queue == "local")
            else _pick_runner(queue)
        )

        # Resolve effective exec_host for repos_root fallback: prefer explicit
        # override, then agent config, then "local".
        if resolved_host:
            _effective_host = resolved_host
        else:
            _agent_cfg = cfg_store.get().agents.get(agent_name)
            _effective_host = (_agent_cfg.exec_host if _agent_cfg and _agent_cfg.exec_host else None) or "local"

        return _delegate_to_agent(
            agent_name,
            agent_runner=agent_runner,
            prompt=prompt,
            files=files,
            params_override=params_override,
            cwd=_default_cwd(_effective_host, cwd),
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            confirm_token=confirm_token,
            workspace_override=workspace,
            exec_host_override=resolved_host,
            tag=tag,
            runner_override=runner_override,
            clarify_rounds=clarify_rounds,
        )

    @app.tool()
    def submit_task(
        argv: list[str] | None = None,
        agent_name: str | None = None,
        prompt: str | None = None,
        label: str = "",
        tag: str | None = None,
        queue: str = "local",
        timeout_seconds: int = 600,
        idempotency_key: str | None = None,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> JobResult:
        """Submit a command or agent invocation as an explicit background job.

        Pass either argv (raw command) or agent_name+prompt (agent dispatch).
        Returns immediately with status='running'. Use get_job_result(job_id)
        to poll. Prefer this over run_command for any job expected to take >30s.

        queue: 'local' (default, lighter) or 'ts' (durable across MCP restarts).
          Use 'ts' when the job is expected to run for minutes and must survive
          a potential MCP server restart.

        tag: opaque label stored on the JobResult. Use list_jobs(tag=...) to
        recover all jobs from a given session after context loss.

        idempotency_key: if set and a non-failed job with this key exists,
        returns the existing job instead of submitting again.
        """
        return _submit_task(
            argv=argv,
            agent_name=agent_name,
            prompt=prompt,
            label=label,
            tag=tag,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            cwd=cwd,
            env_extra=env_extra,
            runner=_pick_runner(queue),
            safety=safety,
            agent_runner=agent_runner,
        )

    # ------------------------------------------------------------------
    # Job management tools
    # ------------------------------------------------------------------

    @app.tool()
    def get_job_status(job_id: str) -> JobResult:
        """Return the current status of a background job (lightweight poll).

        Equivalent to get_job_result but signals intent: you only care about
        whether the job is still running, not about reading its output.
        """
        return _get_job_status(job_id, runner=runner)

    @app.tool()
    def get_job_result(job_id: str) -> JobResult:
        """Return the full result for a background job, with zombie detection.

        Check result.status: 'running' means still in progress, 'completed'
        means success, 'failed' means error (see result.error and result.summary),
        'pending_confirmation' means dangerous command awaiting token.
        raw_output_ref points to the stdout log on disk (not inlined by default).

        Inbox side-effect: stamps seen_at on terminal jobs the first time they
        are read, removing them from the default list_jobs() inbox view.
        """
        return _get_job_result(job_id, runner=runner)

    @app.tool()
    def await_job(job_id: str, poll_interval: float = 60.0) -> JobResult:
        """Block until job_id finishes and return its final result.

        Polls every poll_interval seconds (default 60) on the server side —
        use this instead of a manual get_job_status loop to avoid flooding
        the orchestrator context with intermediate polling calls.
        Stamps seen_at on completion (same as get_job_result).
        """
        return _await_job(job_id, poll_interval=poll_interval, runner=runner)

    @app.tool()
    def list_jobs(
        tag: str | None = None,
        status: list[str] | None = None,
        include_seen: bool = False,
    ) -> list[JobResult]:
        """Return jobs matching the filters — defaults to the inbox view.

        Inbox view (default): active jobs (running/queued/pending_confirmation)
        plus terminal jobs not yet read via get_job_result (seen_at=null).
        This is the answer to "what needs my attention right now?" after a
        context loss or server restart.

        tag: filter by the orchestrator-supplied tag set on submit.
        status: explicit list of statuses to include (overrides inbox filter).
        include_seen: set True to include already-acknowledged terminal jobs.
        """
        return _list_jobs(runner=runner, tag=tag, status=status, include_seen=include_seen)

    @app.tool()
    def cancel_job(job_id: str) -> JobResult:
        """Send SIGTERM to a running job and mark it cancelled.

        If the job is already finished (completed/failed/cancelled) the
        existing result is returned unchanged.
        """
        return _cancel_job(job_id, runner=runner)

    @app.tool()
    def cleanup_jobs(
        older_than: str = "7d",
        keep_unseen: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Evict old job directories from disk.

        older_than: age threshold, e.g. '7d', '30d', '1d'.
        keep_unseen: when True (default), spares terminal jobs not yet read
          via get_job_result — they are still in the inbox.
        dry_run: when True (default), reports what would be removed without
          deleting. Set to False to execute.
        """
        return _cleanup_jobs(runner=runner, older_than=older_than,
                             keep_unseen=keep_unseen, dry_run=dry_run)

    @app.tool()
    def cleanup_branches(
        cwd: str,
        prefix: str = "unlimited-mcp/",
        merged_into: str | None = "main",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Remove leftover unlimited-mcp/* branches from a git repository.

        These accumulate from safe_dev workspace jobs (result: leave_branch).

        cwd: path to the git repository to clean up.
        merged_into: only remove branches already merged into this ref (safe
          default). Pass null to remove all matching branches unconditionally.
        dry_run: when True (default), lists without deleting.
        """
        return _cleanup_branches(
            cwd, prefix=prefix, merged_into=merged_into, dry_run=dry_run,
            work_dir=state_dir() / "work",
        )

    @app.tool()
    def cleanup_state(
        logs: bool = True,
        tmp: bool = True,
        worktrees: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Clean up accumulated state: old log lines, /tmp files, and orphaned worktree dirs.

        Covers the three categories not handled by cleanup_jobs / cleanup_branches:

        logs:      Trim lines older than 30 days from errors.jsonl and exec.jsonl.
        tmp:       Remove items older than 7 days from /tmp/unlimited-mcp.
        worktrees: Remove directories in state/work/ whose source git repo no longer
                   exists on disk. Only these are safe to auto-delete — worktrees
                   whose repo still exists are left untouched regardless of age.

        dry_run (default True): report what would be removed without deleting anything.

        cleanup_jobs and cleanup_branches remain separate tools because they carry
        their own semantics (keep_unseen, merged_into, etc.).
        """
        from unlimited_mcp.observability.startup_cleanup import (
            cleanup_orphaned_worktrees,
            cleanup_tmp,
            trim_jsonl,
        )
        from unlimited_mcp.paths import audit_dir

        report: dict[str, Any] = {"dry_run": dry_run}

        if logs:
            log_report: dict[str, Any] = {}
            for name in ("errors.jsonl", "exec.jsonl"):
                path = audit_dir() / name
                if dry_run:
                    # Count lines that would be removed without touching the file
                    import json as _json
                    from datetime import UTC, datetime, timedelta
                    cutoff = datetime.now(UTC) - timedelta(days=30)
                    count = 0
                    if path.exists():
                        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                entry = _json.loads(raw)
                                ts_raw = entry.get("timestamp") or entry.get("ts") or ""
                                if ts_raw:
                                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                                    if ts < cutoff:
                                        count += 1
                            except (ValueError, _json.JSONDecodeError):
                                pass
                    log_report[name] = {"would_remove_lines": count}
                else:
                    removed = trim_jsonl(path, max_age_days=30)
                    log_report[name] = {"removed_lines": removed}
            report["logs"] = log_report

        if tmp:
            tmp_dir = Path("/tmp/unlimited-mcp")
            if dry_run:
                import time as _time
                cutoff_ts = _time.time() - 7 * 86400
                would_remove = [
                    str(c) for c in tmp_dir.iterdir()
                    if tmp_dir.exists() and c.stat().st_mtime < cutoff_ts
                ] if tmp_dir.exists() else []
                report["tmp"] = {"would_remove": would_remove, "count": len(would_remove)}
            else:
                removed = cleanup_tmp(tmp_dir, max_age_days=7)
                report["tmp"] = {"removed": removed, "count": len(removed)}

        if worktrees:
            work_dir = state_dir() / "work"
            if dry_run:
                would_remove = []
                if work_dir.exists():
                    for entry in work_dir.iterdir():
                        if not entry.is_dir():
                            continue
                        git_file = entry / ".git"
                        if not git_file.is_file():
                            continue
                        content = git_file.read_text(encoding="utf-8", errors="replace").strip()
                        if not content.startswith("gitdir:"):
                            continue
                        from pathlib import PurePosixPath as _PP
                        gitdir = Path(content[len("gitdir:"):].strip())
                        if not gitdir.parent.parent.exists():
                            would_remove.append(str(entry))
                report["worktrees"] = {"would_remove": would_remove, "count": len(would_remove)}
            else:
                removed_wt = cleanup_orphaned_worktrees(work_dir)
                report["worktrees"] = {"removed": removed_wt, "count": len(removed_wt)}

        return report

    @app.tool()
    def cleanup_remote(
        exec_host: str,
        repos: list[str] | None = None,
        branches: bool = True,
        merged_into: str = "main",
        ts_output: bool = True,
        tmp: bool = True,
        older_than_days: int = 7,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Clean up accumulated state on a remote SSH host.

        Covers three categories that build up over time on remote workers:

        branches:   unlimited-mcp/* git branches already merged into `merged_into`.
                    Scans repos under the host's repos_root (or the explicit `repos` list).
        ts_output:  task-spooler output files older than `older_than_days` days.
        tmp:        /tmp/unlimited-mcp directory on the remote, if present.

        exec_host:       Name of a host from config.hosts (must be type: ssh).
        repos:           List of repo names under repos_root to scan for branches.
                         None (default) = discover all git repos under repos_root.
        dry_run:         True (default) — report without deleting. Set False to execute.
        older_than_days: Age threshold for ts_output and tmp cleanup (default 7).
        """
        if host_registry is None:
            return {"ok": False, "error": "No host registry configured."}

        try:
            host = host_registry.get(exec_host)
        except KeyError as e:
            return {"ok": False, "error": str(e)}

        host_cfg = getattr(host, "_config", None)
        host_repos_root: str | None = getattr(host_cfg, "repos_root", None)

        report: dict[str, Any] = {"ok": True, "dry_run": dry_run, "exec_host": exec_host}

        # ---- branches --------------------------------------------------------
        if branches:
            branch_report: dict[str, Any] = {}

            # Discover repos to scan
            repo_names: list[str] = []
            if repos:
                repo_names = repos
            elif host_repos_root:
                ls_out = host.run(["find", host_repos_root, "-maxdepth", "1",
                                   "-mindepth", "1", "-type", "d"])
                if ls_out.exit_code == 0:
                    repo_names = [
                        line.rstrip("/").split("/")[-1]
                        for line in ls_out.stdout.decode().splitlines()
                        if line.strip()
                    ]

            for repo_name in repo_names:
                repo_path = f"{host_repos_root}/{repo_name}" if host_repos_root else repo_name
                # List branches
                list_out = host.run(
                    ["git", "-C", repo_path, "branch",
                     "--format=%(refname:short)", f"--merged={merged_into}"],
                )
                if list_out.exit_code != 0:
                    branch_report[repo_name] = {"error": list_out.stderr.decode().strip()}
                    continue
                candidates = [
                    b.strip() for b in list_out.stdout.decode().splitlines()
                    if b.strip().startswith("unlimited-mcp/") and b.strip() != merged_into
                ]
                if dry_run:
                    branch_report[repo_name] = {"would_delete": candidates, "count": len(candidates)}
                else:
                    deleted, failed = [], []
                    for branch in candidates:
                        del_out = host.run(["git", "-C", repo_path, "branch", "-d", branch])
                        (deleted if del_out.exit_code == 0 else failed).append(branch)
                    branch_report[repo_name] = {"deleted": deleted, "failed": failed,
                                                "count": len(deleted)}

            report["branches"] = branch_report

        # ---- ts output files -------------------------------------------------
        if ts_output:
            find_cmd = [
                "find", "/tmp", "-maxdepth", "1", "-name", "ts-out.*",
                "-mtime", f"+{older_than_days}",
            ]
            find_out = host.run(find_cmd)
            ts_files = [f.strip() for f in find_out.stdout.decode().splitlines() if f.strip()]
            if dry_run:
                report["ts_output"] = {"would_remove": ts_files, "count": len(ts_files)}
            else:
                if ts_files:
                    rm_out = host.run(["rm", "-f", *ts_files])
                    report["ts_output"] = {
                        "removed": ts_files if rm_out.exit_code == 0 else [],
                        "error": rm_out.stderr.decode().strip() if rm_out.exit_code != 0 else None,
                        "count": len(ts_files),
                    }
                else:
                    report["ts_output"] = {"removed": [], "count": 0}

        # ---- remote /tmp/unlimited-mcp ---------------------------------------
        if tmp:
            remote_tmp = "/tmp/unlimited-mcp"
            find_tmp_out = host.run([
                "find", remote_tmp, "-maxdepth", "1", "-mindepth", "1",
                "-mtime", f"+{older_than_days}",
            ])
            if find_tmp_out.exit_code == 0:
                tmp_items = [f.strip() for f in find_tmp_out.stdout.decode().splitlines() if f.strip()]
                if dry_run:
                    report["tmp"] = {"would_remove": tmp_items, "count": len(tmp_items)}
                else:
                    if tmp_items:
                        rm_tmp = host.run(["rm", "-rf", *tmp_items])
                        report["tmp"] = {
                            "removed": tmp_items if rm_tmp.exit_code == 0 else [],
                            "error": rm_tmp.stderr.decode().strip() if rm_tmp.exit_code != 0 else None,
                            "count": len(tmp_items),
                        }
                    else:
                        report["tmp"] = {"removed": [], "count": 0}
            else:
                report["tmp"] = {"skipped": f"{remote_tmp} not found or not accessible"}

        return report

    # ------------------------------------------------------------------
    # Config management tools
    # ------------------------------------------------------------------

    @app.tool()
    def list_capabilities() -> dict[str, Any]:
        """List all configured agents, providers, tools, and the safety policy.

        Call this first after connecting to understand what workers are available
        and which filesystem paths are currently allowed.
        """
        return _list_capabilities(config=cfg_store.get(), knowledge=kn_store.get())

    @app.tool()
    def query_logs(
        since: str | None = None,
        level: str | None = None,
        tool: str | None = None,
        job_id: str | None = None,
        source: str = "server",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query the MCP operational and error logs without needing to know their paths.

        Use this to diagnose failures, trace what happened during a job, or
        check for recent errors — all from within the MCP session.

        since:  Relative duration ("1h", "30m", "2d") or ISO-8601 datetime.
                When omitted, returns the newest `limit` entries regardless of age.
        level:  Filter by log level: "info", "warning", "error".
        tool:   Filter by MCP tool name, e.g. "run_command" or "delegate_to_agent".
        job_id: Filter by job identifier to see everything logged for one job.
        source: "server" (default) — operational log; "errors" — errors.jsonl;
                "audit" — exec.jsonl (redacted argv, exit codes, safety class — no prompts/output);
                "all" — all three.
        limit:  Maximum entries returned (newest wins). Default 50.

        Returns {ok, total_matched, returned, truncated, sources_read, entries}.
        """
        from unlimited_mcp.paths import audit_dir, logs_dir
        return _query_logs(
            logs_dir(),
            audit_dir(),
            since=since,
            level=level,
            tool=tool,
            job_id=job_id,
            source=source,
            limit=limit,
        )

    @app.tool()
    def add_provider(
        name: str,
        provider_type: str,
        model: str,
        base_url: str | None = None,
        api_key_env: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or replace a provider entry in config.yaml.

        provider_type: 'openai_compat' | 'ollama' | 'anthropic'
        api_key_env: name of the env var holding the API key (e.g. 'OPENAI_API_KEY')

        Example: add_provider(name='opencode_default', provider_type='openai_compat',
                   model='deepseek-v3', base_url='https://opencode.ai/zen/go/v1',
                   api_key_env='OPENCODE_API_KEY')
        """
        return _add_provider(
            name, provider_type, model,
            base_url=base_url, api_key_env=api_key_env, tags=tags,
            config_store=cfg_store,
        )

    @app.tool()
    def add_agent(
        name: str,
        cli: str,
        tags: list[str] | None = None,
        suitable_for: list[str] | None = None,
        workspace: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add or replace an agent entry in config.yaml.

        cli must match a key in knowledge.clis (e.g. 'aider', 'opencode').
        workspace: a preset name like 'safe_dev' or null for default.
        params: initial params dict (e.g. {'model': 'gpt-4o', 'git': True}).

        Use lookup_agent_cli(cli) first to see available params and install hints.
        """
        return _add_agent(
            name, cli,
            tags=tags, suitable_for=suitable_for, workspace=workspace, params=params,
            config_store=cfg_store,
        )

    @app.tool()
    def configure_agent(
        name: str,
        set: dict[str, Any] | None = None,  # noqa: A002
        unset: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update params or top-level fields on an existing agent.

        set: mapping of field → value to apply (params or top-level like 'workspace').
        unset: list of param keys to remove from the params sub-dict.

        Example: configure_agent('aider_local', set={'model': 'gpt-4o', 'git': True})
        """
        return _configure_agent(name, set=set, unset=unset, config_store=cfg_store)

    @app.tool()
    def configure_safety(
        allow_shell_like_argv: bool | None = None,
        default_safety_policy: str | None = None,
        confirm_token_ttl_seconds: int | None = None,
        log_full_shell_scripts: bool | None = None,
        clarify_max_rounds: int | None = None,
        clarify_max_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Update global safety and clarify settings in config.yaml.

        All parameters are optional — only the ones provided are changed.
        Changes take effect on the next tool call (config is re-read live).

        allow_shell_like_argv:     Allow argv like ['bash', '-c', '...']. Default False.
        default_safety_policy:     'read_only' | 'standard' | 'permissive'.
        confirm_token_ttl_seconds: Token validity window in seconds (default 300).
        log_full_shell_scripts:    Log full script content in audit log (default False).
        clarify_max_rounds:        Cap on clarify_rounds per task (default 5).
        clarify_max_seconds:       Total Q&A wait budget in seconds (default 300).

        Example: configure_safety(allow_shell_like_argv=True, default_safety_policy='permissive')
        """
        return _configure_safety(
            allow_shell_like_argv=allow_shell_like_argv,
            default_safety_policy=default_safety_policy,
            confirm_token_ttl_seconds=confirm_token_ttl_seconds,
            log_full_shell_scripts=log_full_shell_scripts,
            clarify_max_rounds=clarify_max_rounds,
            clarify_max_seconds=clarify_max_seconds,
            config_store=cfg_store,
        )

    @app.tool()
    def add_host(
        name: str,
        host: str,
        user: str,
        port: int = 22,
        key_file: str | None = None,
        key_passphrase_env: str | None = None,
        key_passphrase_keyring: str | None = None,
        key_passphrase_account: str | None = None,
    ) -> dict[str, Any]:
        """Add or replace an SSH host in config.yaml.

        The host is immediately usable for run_command(exec_host=name) and
        delegate_to_agent(exec_host=name) without a server restart.

        name:    Identifier used in exec_host / queue host field (e.g. 'gpu_server').
        host:    Hostname or IP of the remote machine.
        user:    SSH username.
        port:    SSH port (default 22).
        key_file: Path to a specific private key (optional — agent/default keys are tried first).
        key_passphrase_env: Name of an env var holding the key passphrase (never the value itself).
        key_passphrase_keyring: Keychain service name holding the passphrase.
        key_passphrase_account: Keychain account for the passphrase lookup.
            Defaults to the key_file basename (e.g. 'id_rsa'), falling back to
            the SSH user. Set this so several hosts sharing one private key can
            reuse a single keychain entry.

        Call ssh_trust_host(host, port) first if the machine is not yet in known_hosts.
        Example: add_host('gpu_server', host='192.168.1.100', user='ubuntu')
        """
        return _add_host(
            name, host, user,
            port=port,
            key_file=key_file,
            key_passphrase_env=key_passphrase_env,
            key_passphrase_keyring=key_passphrase_keyring,
            key_passphrase_account=key_passphrase_account,
            config_store=cfg_store,
        )

    @app.tool()
    def add_queue(
        name: str,
        queue_type: str = "remote_ts",
        slots: int = 1,
        host: str | None = None,
        socket: str | None = None,
    ) -> dict[str, Any]:
        """Add or replace a queue entry in config.yaml.

        queue_type: 'remote_ts' — task-spooler on a remote SSH host (most common).
                    'local_ts'  — task-spooler on this machine.
        host:   SSH host name (key in hosts:) — required for remote_ts.
        slots:  Maximum simultaneous jobs on the remote ts daemon (default 1).
        socket: Optional TS_SOCKET path on the remote machine (for queue isolation).

        Requires restart_server() to activate (queues are wired at startup).
        Example: add_queue('gpu', host='gpu_server', slots=4)
        """
        return _add_queue(
            name,
            queue_type=queue_type,
            slots=slots,
            host=host,
            socket=socket,
            config_store=cfg_store,
        )

    @app.tool()
    def ssh_trust_host(host: str, port: int = 22) -> dict[str, Any]:
        """Add the SSH host key to ~/.ssh/known_hosts via ssh-keyscan (one-time setup).

        Run this once per new SSH host before add_host / run_command(exec_host=...).
        Without it, paramiko rejects the connection because the host fingerprint
        is unknown.

        WARNING: the fingerprint is added without manual verification.
        Only use on networks you trust (home, VPN, private cloud).

        After this call, test with: run_command(['hostname'], exec_host='<name>')
        """
        return _ssh_trust_host(host, port=port)

    @app.tool()
    def remove_entry(section: str, name: str) -> dict[str, Any]:
        """Remove a named entry from a config section.

        section: 'agents' | 'providers' | 'hosts' | 'queues'
        """
        return _remove_entry(section, name, config_store=cfg_store)

    @app.tool()
    def list_safety_policy() -> dict[str, Any]:
        """Return the current safety configuration, allowed_roots and deny_paths."""
        return _list_safety_policy(config=cfg_store.get())

    @app.tool()
    def add_allowed_root(path: str) -> dict[str, Any]:
        """Add a filesystem path to allowed_roots.

        Workers and run_command can only access paths inside allowed_roots.
        Call this before delegating any task that needs to write to a repo.
        Example: add_allowed_root('/Users/me/projects/my-repo')
        """
        return _add_allowed_root(path, config_store=cfg_store)

    @app.tool()
    def remove_allowed_root(path: str) -> dict[str, Any]:
        """Remove a filesystem path from allowed_roots."""
        return _remove_allowed_root(path, config_store=cfg_store)

    @app.tool()
    def add_deny_path(path: str) -> dict[str, Any]:
        """Add a path to deny_paths (always blocked, even if inside an allowed root).

        Example: add_deny_path('/Users/me/projects/my-repo/.env')
        """
        return _add_deny_path(path, config_store=cfg_store)

    @app.tool()
    def remove_deny_path(path: str) -> dict[str, Any]:
        """Remove a path from deny_paths."""
        return _remove_deny_path(path, config_store=cfg_store)

    # ------------------------------------------------------------------
    # Knowledge tools
    # ------------------------------------------------------------------

    @app.tool()
    def lookup_agent_cli(name: str) -> dict[str, Any]:
        """Look up a CLI agent in the knowledge catalog.

        Returns the entry (command_template, params, install_hint) or an error
        with a list of known CLIs. Use this before add_agent to verify the CLI
        is catalogued and discover its params.
        """
        return _lookup_agent_cli(name, knowledge=kn_store.get())

    @app.tool()
    def register_agent_knowledge(
        name: str,
        command_template: str,
        docs_url: str | None = None,
        install_hint: str | None = None,
        params: dict[str, Any] | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        """Write a new CLI entry to knowledge.local.yaml (gitignored user catalog).

        Use this to teach the server about a CLI that's not in the repo catalog.
        After registration, call add_agent(name, cli=name) to create a runnable agent.

        command_template examples:
          'goose run {prompt!q}'
          'hermes --task {prompt!q} --workspace {cwd}'
        """
        return _register_agent_knowledge(
            name, command_template,
            docs_url=docs_url, install_hint=install_hint,
            params=params, verified=verified,
            knowledge_local_path=kl_path,
        )

    # ------------------------------------------------------------------
    # Worker clarification tools
    # ------------------------------------------------------------------

    @app.tool()
    def get_worker_questions(job_id: str) -> dict[str, Any]:
        """Return all clarification rounds for a job and their answered status.

        Call this when a job was started with clarify_rounds > 0 and you want
        to see what questions the agent wrote before starting work.  Returns
        pending_round (the round number waiting for answers) or None if all
        rounds are answered.  Also indicates timed_out=true if the agent
        exhausted its wait budget.

        The response includes poll_interval_hint (seconds):
          - 0  → pending_round is set, call answer_worker_questions immediately.
          - 5  → no questions yet, wait this long before polling again.
        Do NOT poll in a tight loop — the agent syncs files every ~3 s so
        polling faster than poll_interval_hint wastes tokens with no benefit.

        Example flow:
          result = get_worker_questions(job_id)
          if result['pending_round']:
              answer_worker_questions(job_id, result['pending_round'], [...])
          elif not result['timed_out']:
              # wait poll_interval_hint seconds, then try again
        """
        return _get_worker_questions(job_id, runner=runner)

    @app.tool()
    def answer_worker_questions(
        job_id: str,
        round_number: int,
        answers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Write answers for a clarification round, unblocking the waiting agent.

        Each answer must have 'id' (matching the question id) and 'answer'.
        An optional 'reasoning' field is preserved in the Q&A history.

        To stop the Q&A phase early, pass {"id": N, "answer": "STOP"} — the
        agent will proceed immediately with what it already knows.

        Example:
          answer_worker_questions(
              job_id="delegate_to_agent-...",
              round_number=1,
              answers=[
                  {"id": 1, "answer": "B: Redis sessions", "reasoning": "Force-logout required"},
                  {"id": 2, "answer": "A: argon2id"},
              ],
          )
        """
        return _answer_worker_questions(job_id, round_number, answers, runner=runner)

    @app.tool()
    def resume_agent_task(
        failed_job_id: str,
        extra_context: str | None = None,
        agent_name_override: str | None = None,
        clarify_rounds: int = 0,
    ) -> JobResult:
        """Relaunch a failed clarify-phase job with the Q&A history injected.

        Use when a job exits with code 2 (agent timed out waiting for answers).
        Reads the full Q&A history from the failed job's questions/ directory,
        builds an enriched prompt with all decisions made so far, and submits
        a new job to the same agent (or agent_name_override).

        extra_context: optional text appended after the history — use this to
          provide the answer to the question that caused the timeout.
        clarify_rounds: allow additional clarification rounds in the resumed
          job (default 0 — the history is already embedded in the prompt).
        """
        return _resume_agent_task(
            failed_job_id,
            runner=runner,
            agent_runner=agent_runner,
            extra_context=extra_context,
            agent_name_override=agent_name_override,
            clarify_rounds=clarify_rounds,
        )

    # ------------------------------------------------------------------
    # Meta tools
    # ------------------------------------------------------------------

    @app.tool()
    async def restart_server() -> dict[str, Any]:
        """Restart the MCP server process (re-exec with the same argv).

        Use after install_and_restart or after changing config.yaml manually.
        The orchestrator will see the stdio connection drop and reconnect.
        Refuses if there are jobs currently in 'running' status.
        """
        running_jobs = [j for j in runner.list_results() if j.status == "running"]
        if running_jobs:
            ids = [j.job_id for j in running_jobs]
            return {
                "ok": False,
                "message": f"Cannot restart: {len(ids)} job(s) still running: {ids}",
            }

        def _do_execv() -> None:
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.get_event_loop().call_later(0.5, _do_execv)
        return {"ok": True, "message": "Restarting in 0.5 s — connection will drop briefly."}

    @app.tool()
    async def install_and_restart(package: str) -> dict[str, Any]:
        """Install a Python package with uv/pip then restart the server.

        package: pip-style specifier, e.g. 'aider-install' or 'unlimited-mcp>=0.2'.
        Refuses if jobs are running (same gate as restart_server).
        """
        running_jobs = [j for j in runner.list_results() if j.status == "running"]
        if running_jobs:
            ids = [j.job_id for j in running_jobs]
            return {
                "ok": False,
                "message": f"Cannot install: {len(ids)} job(s) still running: {ids}",
            }
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "message": f"pip install failed: {result.stderr.strip()[:500]}",
            }

        def _do_execv() -> None:
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.get_event_loop().call_later(0.5, _do_execv)
        return {"ok": True, "message": "Installed — restarting in 0.5 s."}

    # ------------------------------------------------------------------
    # Startup cleanup — runs once when the server initialises, all non-fatal
    # ------------------------------------------------------------------
    import logging as _logging
    from unlimited_mcp.observability.startup_cleanup import (
        cleanup_orphaned_worktrees,
        cleanup_tmp,
        trim_jsonl,
    )
    from unlimited_mcp.paths import audit_dir, logs_dir

    _startup_log = _logging.getLogger(__name__)

    # 1. Old job directories (>7 days, seen by orchestrator)
    try:
        evicted = job_store.cleanup_older_than(7, keep_unseen=True)
        if evicted:
            _startup_log.info("Startup cleanup: removed %d old job(s)", len(evicted))
    except Exception as _exc:
        _startup_log.warning("Startup cleanup jobs failed (non-fatal): %s", _exc)

    # 2. Worktree dirs whose source repo no longer exists
    try:
        orphans = cleanup_orphaned_worktrees(state_dir() / "work")
        if orphans:
            _startup_log.info("Startup cleanup: removed %d orphaned worktree(s): %s", len(orphans), orphans)
    except Exception as _exc:
        _startup_log.warning("Startup cleanup worktrees failed (non-fatal): %s", _exc)

    # 3. Trim old lines from JSONL audit logs (keep 30 days)
    for _log_path in (audit_dir() / "errors.jsonl", audit_dir() / "exec.jsonl"):
        try:
            _removed = trim_jsonl(_log_path, max_age_days=30)
            if _removed:
                _startup_log.info("Startup cleanup: trimmed %d old line(s) from %s", _removed, _log_path.name)
        except Exception as _exc:
            _startup_log.warning("Startup cleanup trim %s failed (non-fatal): %s", _log_path.name, _exc)

    # 4. /tmp/unlimited-mcp — files/dirs older than 7 days
    try:
        _tmp_removed = cleanup_tmp(Path("/tmp/unlimited-mcp"), max_age_days=7)
        if _tmp_removed:
            _startup_log.info("Startup cleanup: removed %d item(s) from /tmp/unlimited-mcp", len(_tmp_removed))
    except Exception as _exc:
        _startup_log.warning("Startup cleanup /tmp failed (non-fatal): %s", _exc)

    return app
