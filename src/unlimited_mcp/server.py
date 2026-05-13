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

import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from unlimited_mcp.agents.runner import AgentRunner
from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.jobs.result import JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.paths import (
    config_path,
    jobs_dir,
    knowledge_local_path,
)
from unlimited_mcp.providers.base import Provider
from unlimited_mcp.safety.argv_check import SafetyChecker
from unlimited_mcp.tools.config_tools import (
    add_agent as _add_agent,
    add_allowed_root as _add_allowed_root,
    add_deny_path as _add_deny_path,
    add_provider as _add_provider,
    configure_agent as _configure_agent,
    list_capabilities as _list_capabilities,
    list_safety_policy as _list_safety_policy,
    remove_allowed_root as _remove_allowed_root,
    remove_deny_path as _remove_deny_path,
    remove_entry as _remove_entry,
)
from unlimited_mcp.tools.execution import delegate_to_agent as _delegate_to_agent
from unlimited_mcp.tools.execution import run_and_summarize as _run_and_summarize
from unlimited_mcp.tools.execution import run_command as _run_command
from unlimited_mcp.tools.jobs import cancel_job as _cancel_job
from unlimited_mcp.tools.jobs import get_job_result as _get_job_result
from unlimited_mcp.tools.jobs import get_job_status as _get_job_status
from unlimited_mcp.tools.jobs import list_jobs as _list_jobs
from unlimited_mcp.tools.jobs import submit_task as _submit_task
from unlimited_mcp.tools.knowledge_tools import (
    lookup_agent_cli as _lookup_agent_cli,
    register_agent_knowledge as _register_agent_knowledge,
)

_REPO_KNOWLEDGE_PATH = Path(__file__).parent.parent.parent / "knowledge.yaml"


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
    # Pass stores (not snapshots) so config changes made via MCP tools
    # (add_agent, add_allowed_root, …) are visible on the next tool call.
    safety = SafetyChecker(cfg_store, kn_store)
    agent_runner = AgentRunner(config=cfg_store, knowledge=kn_store, local_runner=runner, safety=safety)

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
    ) -> JobResult:
        """Run a command after the safety pipeline.

        argv must be a list — no shell interpolation. Returns immediately with
        status='running'. Poll with get_job_result(job_id) to check completion.

        Safety blocks return status='failed' (OUT_OF_ROOT, SHELL_LIKE_BLOCKED) or
        status='pending_confirmation' with a confirm_token for dangerous commands.
        Re-call with confirm_token=<token> to proceed after user approval.
        """
        return _run_command(
            argv,
            safety=safety,
            runner=runner,
            cwd=cwd,
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            confirm_token=confirm_token,
        )

    @app.tool()
    def run_and_summarize(
        argv: list[str],
        cwd: str | None = None,
        timeout_seconds: int = 600,
        confirm_token: str | None = None,
    ) -> JobResult:
        """Run a command, wait for completion, then summarise stdout via the provider.

        Blocks (polls internally) until the job finishes. Useful for short
        commands where you want the output digested into summary rather than
        reading raw output. If no provider is configured, returns the raw result.
        """
        return _run_and_summarize(
            argv,
            safety=safety,
            runner=runner,
            provider=provider,
            model=provider_model,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            confirm_token=confirm_token,
        )

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
    ) -> JobResult:
        """Delegate a coding task to a configured agent (aider, opencode, smolagents, ...).

        agent_name must match a key in config.agents. The agent runs in the
        background; poll with get_job_result(job_id). Use workspace preset
        'safe_dev' (git_worktree + leave_branch) for in-repo write tasks so
        the agent never touches the main working tree directly.

        Example: delegate_to_agent(agent_name='aider_local',
                   prompt='add docstrings to all public functions',
                   cwd='/path/to/repo')
        """
        return _delegate_to_agent(
            agent_name,
            agent_runner=agent_runner,
            prompt=prompt,
            files=files,
            params_override=params_override,
            cwd=cwd,
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            confirm_token=confirm_token,
        )

    @app.tool()
    def submit_task(
        argv: list[str] | None = None,
        agent_name: str | None = None,
        prompt: str | None = None,
        label: str = "",
        timeout_seconds: int = 600,
        idempotency_key: str | None = None,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> JobResult:
        """Submit a command or agent invocation as an explicit background job.

        Pass either argv (raw command) or agent_name+prompt (agent dispatch).
        Returns immediately with status='running'. Use get_job_result(job_id)
        to poll. Prefer this over run_command for any job expected to take >30s.

        idempotency_key: if set and a non-failed job with this key exists,
        returns the existing job instead of submitting again.
        """
        return _submit_task(
            argv=argv,
            agent_name=agent_name,
            prompt=prompt,
            label=label,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            cwd=cwd,
            env_extra=env_extra,
            runner=runner,
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
        """
        return _get_job_result(job_id, runner=runner)

    @app.tool()
    def list_jobs() -> list[JobResult]:
        """Return all known job results, ordered by submission time.

        Useful to audit what workers ran and their final status. Failed jobs
        include a summary of the last stderr line.
        """
        return _list_jobs(runner=runner)

    @app.tool()
    def cancel_job(job_id: str) -> JobResult:
        """Send SIGTERM to a running job and mark it cancelled.

        If the job is already finished (completed/failed/cancelled) the
        existing result is returned unchanged.
        """
        return _cancel_job(job_id, runner=runner)

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
    # Meta tools
    # ------------------------------------------------------------------

    @app.tool()
    def restart_server() -> dict[str, Any]:
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
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return {"ok": True, "message": "Restarting..."}  # unreachable

    @app.tool()
    def install_and_restart(package: str) -> dict[str, Any]:
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
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return {"ok": True, "message": "Installed, restarting..."}  # unreachable

    return app
