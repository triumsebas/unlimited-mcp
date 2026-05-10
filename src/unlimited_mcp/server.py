"""MCP server factory for unlimited-mcp.

Wires the pure tool functions from :mod:`unlimited_mcp.tools.execution` into
a :class:`~mcp.server.fastmcp.FastMCP` instance ready to be served over stdio.

Lifetime model
--------------
All stateful objects (``LocalRunner``, ``SafetyChecker``, ``AgentRunner``) are
created once inside :func:`make_server` and shared across all tool calls via
closure.  The ``SafetyChecker`` holds the ``ConfirmationStore``, so
confirmation tokens persist within a single server lifetime.  Live reload of
config/knowledge is deferred to a future phase.

Usage::

    from unlimited_mcp.server import make_server
    app = make_server()
    app.run()                  # blocks, stdio transport
"""

from __future__ import annotations

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
from unlimited_mcp.tools.execution import delegate_to_agent as _delegate_to_agent
from unlimited_mcp.tools.execution import run_and_summarize as _run_and_summarize
from unlimited_mcp.tools.execution import run_command as _run_command
from unlimited_mcp.tools.jobs import cancel_job as _cancel_job
from unlimited_mcp.tools.jobs import get_job_result as _get_job_result
from unlimited_mcp.tools.jobs import list_jobs as _list_jobs

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
    cfg = ConfigStore(config_file or config_path()).get()
    kn = KnowledgeStore(
        knowledge_repo or _REPO_KNOWLEDGE_PATH,
        knowledge_local or knowledge_local_path(),
    ).get()

    job_store = JobStore(jobs_path or jobs_dir())
    runner = LocalRunner(job_store)
    safety = SafetyChecker(cfg, kn)
    agent_runner = AgentRunner(config=cfg, knowledge=kn, local_runner=runner, safety=safety)

    app = FastMCP(server_name)

    @app.tool()
    def run_command(
        argv: list[str],
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        confirm_token: str | None = None,
    ) -> JobResult:
        """Run an arbitrary command after the safety pipeline."""
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
        """Resolve an agent name and dispatch the job via AgentRunner."""
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
    def run_and_summarize(
        argv: list[str],
        cwd: str | None = None,
        timeout_seconds: int = 600,
        confirm_token: str | None = None,
    ) -> JobResult:
        """Run a command, poll until done, then summarise output via the provider."""
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
    def get_job_result(job_id: str) -> JobResult:
        """Return the current result for a background job (with zombie detection)."""
        return _get_job_result(job_id, runner=runner)

    @app.tool()
    def list_jobs() -> list[JobResult]:
        """Return all known job results, ordered by submission time."""
        return _list_jobs(runner=runner)

    @app.tool()
    def cancel_job(job_id: str) -> JobResult:
        """Send SIGTERM to a running job and mark it cancelled."""
        return _cancel_job(job_id, runner=runner)

    return app
