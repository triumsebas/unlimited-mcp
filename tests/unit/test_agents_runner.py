"""Unit tests for agents/runner.py — :class:`AgentRunner.submit`.

The tests build real :class:`Config` / :class:`Knowledge` instances and a
real :class:`LocalRunner` backed by a tmp_path :class:`JobStore`.  All
subprocesses are spawned via ``/bin/echo`` so the runner is exercised
end-to-end without requiring aider/opencode/etc. to be installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unlimited_mcp.agents.base import AgentRenderError
from unlimited_mcp.agents.runner import DEFAULT_TOOL_NAME, AgentRunner
from unlimited_mcp.config.schema import (
    AgentConfig,
    CliKnowledge,
    Config,
    Knowledge,
    ParamSpec,
    ToolKnowledge,
)
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.argv_check import SafetyChecker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    allowed_roots: list[str] | None = None,
    agents: dict[str, dict[str, Any]] | None = None,
) -> Config:
    return Config(
        allowed_roots=allowed_roots or [],
        agents={n: AgentConfig(**c) for n, c in (agents or {}).items()},
    )


def _knowledge(
    *,
    clis: dict[str, CliKnowledge] | None = None,
    tools: dict[str, ToolKnowledge] | None = None,
) -> Knowledge:
    return Knowledge(clis=clis or {}, tools=tools or {})


def _echo_cli() -> CliKnowledge:
    """Trivial CLI: ``/bin/echo {prompt}``."""
    return CliKnowledge(command_template="/bin/echo {prompt}")


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


@pytest.fixture()
def local_runner(store: JobStore) -> LocalRunner:
    return LocalRunner(store)


def _make_runner(
    *,
    config: Config,
    knowledge: Knowledge,
    local_runner: LocalRunner,
) -> AgentRunner:
    return AgentRunner(
        config=config,
        knowledge=knowledge,
        local_runner=local_runner,
        safety=SafetyChecker(config, knowledge),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_returns_running_job(local_runner: LocalRunner) -> None:
    cfg = _config(agents={"a": {"cli": "echo"}})
    kn = _knowledge(clis={"echo": _echo_cli()})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="hello")
    assert result.status == "running"
    assert result.tool == DEFAULT_TOOL_NAME
    assert result.job_id
    local_runner.join_all()


def test_submit_dispatches_rendered_argv(local_runner: LocalRunner, store: JobStore) -> None:
    cfg = _config(agents={"a": {"cli": "echo"}})
    kn = _knowledge(clis={"echo": _echo_cli()})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="dispatched-prompt-marker")
    local_runner.join_all()
    stdout = store.stdout_path(result.job_id).read_bytes()
    assert b"dispatched-prompt-marker" in stdout


def test_custom_tool_name_passes_through(local_runner: LocalRunner) -> None:
    cfg = _config(agents={"a": {"cli": "echo"}})
    kn = _knowledge(clis={"echo": _echo_cli()})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="hi", tool="run_and_summarize")
    assert result.tool == "run_and_summarize"
    local_runner.join_all()


# ---------------------------------------------------------------------------
# Render errors propagate as AgentRenderError
# ---------------------------------------------------------------------------


def test_unknown_agent_raises(local_runner: LocalRunner) -> None:
    cfg = _config()
    kn = _knowledge()
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)
    with pytest.raises(AgentRenderError, match="Unknown agent"):
        runner.submit("missing", prompt="hi")


def test_unknown_cli_raises(local_runner: LocalRunner) -> None:
    cfg = _config(agents={"a": {"cli": "ghost"}})
    kn = _knowledge(clis={"echo": _echo_cli()})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)
    with pytest.raises(AgentRenderError, match="unknown CLI"):
        runner.submit("a", prompt="hi")


# ---------------------------------------------------------------------------
# Safety: hard block (OUT_OF_ROOT)
# ---------------------------------------------------------------------------


def test_out_of_root_returns_failed(local_runner: LocalRunner, tmp_path: Path) -> None:
    # The template includes /etc/passwd, which is outside the single
    # allowed root (tmp_path).  Bare-path detection picks it up; no tools
    # entry is needed.
    cfg = _config(allowed_roots=[str(tmp_path)], agents={"a": {"cli": "cat"}})
    kn = _knowledge(clis={"cat": CliKnowledge(command_template="/bin/cat /etc/passwd")})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a")
    assert result.status == "failed"
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "OUT_OF_ROOT"


# ---------------------------------------------------------------------------
# Safety: confirmation flow
# ---------------------------------------------------------------------------


def test_dangerous_returns_pending_confirmation(local_runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)], agents={"a": {"cli": "echo"}})
    kn = _knowledge(
        clis={"echo": _echo_cli()},
        tools={"echo": ToolKnowledge(safety_class="dangerous")},
    )
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="hi")
    assert result.status == "pending_confirmation"
    assert result.confirm_token
    assert result.confirm_reason
    assert result.risk_level == "high"
    assert result.error is None


def test_confirm_token_unlocks_dispatch(local_runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)], agents={"a": {"cli": "echo"}})
    kn = _knowledge(
        clis={"echo": _echo_cli()},
        tools={"echo": ToolKnowledge(safety_class="dangerous")},
    )
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    first = runner.submit("a", prompt="hi")
    assert first.confirm_token is not None

    second = runner.submit("a", prompt="hi", confirm_token=first.confirm_token)
    assert second.status == "running"
    local_runner.join_all()


def test_invalid_confirm_token_returns_expired(local_runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)], agents={"a": {"cli": "echo"}})
    kn = _knowledge(
        clis={"echo": _echo_cli()},
        tools={"echo": ToolKnowledge(safety_class="dangerous")},
    )
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="hi", confirm_token="not-a-real-token")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "CONFIRMATION_EXPIRED"


# ---------------------------------------------------------------------------
# Pass-through behaviour
# ---------------------------------------------------------------------------


def test_idempotency_key_passes_through(local_runner: LocalRunner) -> None:
    cfg = _config(agents={"a": {"cli": "echo"}})
    kn = _knowledge(clis={"echo": _echo_cli()})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    r1 = runner.submit("a", prompt="hi", idempotency_key="agent-key")
    r2 = runner.submit("a", prompt="hi", idempotency_key="agent-key")
    assert r1.job_id == r2.job_id
    local_runner.join_all()


def test_params_override_reaches_argv(local_runner: LocalRunner, store: JobStore) -> None:
    cli = CliKnowledge(
        command_template="/bin/echo {prompt}",
        params={
            "tag": ParamSpec(type="str", default="default-tag", render="--tag {value}"),
        },
    )
    cfg = _config(agents={"a": {"cli": "echo"}})
    kn = _knowledge(clis={"echo": cli})
    runner = _make_runner(config=cfg, knowledge=kn, local_runner=local_runner)

    result = runner.submit("a", prompt="hi", params_override={"tag": "override-tag"})
    local_runner.join_all()
    stdout = store.stdout_path(result.job_id).read_bytes()
    assert b"override-tag" in stdout
    assert b"default-tag" not in stdout
