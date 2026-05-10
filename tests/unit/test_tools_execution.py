"""Unit tests for tools/execution.py.

All subprocess work uses ``/bin/echo`` so no real agents need to be installed.
``run_and_summarize`` is exercised with ``_poll_interval=0`` to keep tests fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unlimited_mcp.agents.base import AgentRenderError
from unlimited_mcp.agents.runner import AgentRunner
from unlimited_mcp.config.schema import (
    AgentConfig,
    CliKnowledge,
    Config,
    Knowledge,
    ToolKnowledge,
)
from unlimited_mcp.jobs.result import JobResult
from unlimited_mcp.jobs.runner_local import LocalRunner
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.providers.base import ProviderAuthError
from unlimited_mcp.safety.argv_check import SafetyChecker
from unlimited_mcp.tools.execution import delegate_to_agent, run_and_summarize, run_command

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(
    *,
    allowed_roots: list[str] | None = None,
    agents: dict | None = None,
) -> Config:
    return Config(
        allowed_roots=allowed_roots or [],
        agents={n: AgentConfig(**c) for n, c in (agents or {}).items()},
    )


def _knowledge(*, tools: dict[str, ToolKnowledge] | None = None) -> Knowledge:
    return Knowledge(
        clis={"echo": CliKnowledge(command_template="/bin/echo {prompt}")},
        tools=tools or {},
    )


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


@pytest.fixture()
def runner(store: JobStore) -> LocalRunner:
    return LocalRunner(store)


@pytest.fixture()
def safety(tmp_path: Path) -> SafetyChecker:
    cfg = _config(allowed_roots=[str(tmp_path)])
    return SafetyChecker(cfg, _knowledge())


@pytest.fixture()
def agent_runner(tmp_path: Path, runner: LocalRunner) -> AgentRunner:
    cfg = _config(
        allowed_roots=[str(tmp_path)],
        agents={"echo_agent": {"cli": "echo"}},
    )
    kn = _knowledge()
    return AgentRunner(config=cfg, knowledge=kn, local_runner=runner, safety=SafetyChecker(cfg, kn))


# ---------------------------------------------------------------------------
# run_command — happy path
# ---------------------------------------------------------------------------


def test_run_command_allowed_returns_running(safety: SafetyChecker, runner: LocalRunner) -> None:
    result = run_command(["/bin/echo", "hi"], safety=safety, runner=runner)
    assert result.status == "running"
    assert result.ok is False  # "running" → ok=False until completed
    assert result.tool == "run_command"
    runner.join_all()


def test_run_command_custom_tool_name(safety: SafetyChecker, runner: LocalRunner) -> None:
    result = run_command(["/bin/echo", "hi"], safety=safety, runner=runner, tool="my_tool")
    assert result.tool == "my_tool"
    runner.join_all()


# ---------------------------------------------------------------------------
# run_command — safety hard blocks
# ---------------------------------------------------------------------------


def test_run_command_empty_argv_is_hard_block(safety: SafetyChecker, runner: LocalRunner) -> None:
    result = run_command([], safety=safety, runner=runner)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "SHELL_LIKE_BLOCKED"


def test_run_command_out_of_root_is_hard_block(runner: LocalRunner, tmp_path: Path) -> None:
    # /etc/passwd starts with "/" — bare-path detection picks it up automatically.
    cfg = _config(allowed_roots=[str(tmp_path)])
    kn = _knowledge()
    safety = SafetyChecker(cfg, kn)

    result = run_command(["/bin/cat", "/etc/passwd"], safety=safety, runner=runner)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "OUT_OF_ROOT"


# ---------------------------------------------------------------------------
# run_command — confirmation flow
# ---------------------------------------------------------------------------


def test_run_command_dangerous_returns_pending(runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)])
    kn = _knowledge(tools={"echo": ToolKnowledge(safety_class="dangerous")})
    safety = SafetyChecker(cfg, kn)

    result = run_command(["/bin/echo", "hi"], safety=safety, runner=runner)
    assert result.status == "pending_confirmation"
    assert result.confirm_token is not None
    assert result.confirm_reason is not None
    assert result.error is None


def test_run_command_valid_confirm_token_proceeds(runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)])
    kn = _knowledge(tools={"echo": ToolKnowledge(safety_class="dangerous")})
    safety = SafetyChecker(cfg, kn)

    first = run_command(["/bin/echo", "hi"], safety=safety, runner=runner)
    assert first.confirm_token is not None

    second = run_command(
        ["/bin/echo", "hi"], safety=safety, runner=runner, confirm_token=first.confirm_token
    )
    assert second.status == "running"
    runner.join_all()


def test_run_command_invalid_token_returns_expired(runner: LocalRunner, tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)])
    kn = _knowledge(tools={"echo": ToolKnowledge(safety_class="dangerous")})
    safety = SafetyChecker(cfg, kn)

    result = run_command(["/bin/echo", "hi"], safety=safety, runner=runner, confirm_token="bogus")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "CONFIRMATION_EXPIRED"


# ---------------------------------------------------------------------------
# delegate_to_agent
# ---------------------------------------------------------------------------


def test_delegate_passthrough_to_agent_runner(
    agent_runner: AgentRunner, runner: LocalRunner
) -> None:
    result = delegate_to_agent("echo_agent", agent_runner=agent_runner, prompt="hello")
    assert result.status == "running"
    runner.join_all()


def test_delegate_unknown_agent_raises(agent_runner: AgentRunner) -> None:
    with pytest.raises(AgentRenderError):
        delegate_to_agent("no_such_agent", agent_runner=agent_runner)


def test_delegate_idempotency_key_deduplicates(
    agent_runner: AgentRunner, runner: LocalRunner
) -> None:
    r1 = delegate_to_agent(
        "echo_agent", agent_runner=agent_runner, prompt="hi", idempotency_key="k1"
    )
    r2 = delegate_to_agent(
        "echo_agent", agent_runner=agent_runner, prompt="hi", idempotency_key="k1"
    )
    assert r1.job_id == r2.job_id
    runner.join_all()


# ---------------------------------------------------------------------------
# run_and_summarize helpers
# ---------------------------------------------------------------------------


def _make_provider(reply: str = "summary text") -> MagicMock:
    p = MagicMock()
    p.complete.return_value = reply
    return p


def _completed_result() -> JobResult:
    return JobResult(
        ok=True,
        job_id="test-job-id",
        status="completed",
        tool="run_and_summarize",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        summary="Completed successfully.",
    )


def _running_result() -> JobResult:
    return JobResult(
        ok=False,
        job_id="test-job-id",
        status="running",
        tool="run_and_summarize",
        started_at=datetime.now(UTC),
    )


def _mock_runner() -> MagicMock:
    """Runner that submit→running, get_result→completed (no real subprocess)."""
    r = MagicMock(spec=LocalRunner)
    r.submit.return_value = _running_result()
    r.get_result.return_value = _completed_result()
    return r


# ---------------------------------------------------------------------------
# run_and_summarize — no provider
# ---------------------------------------------------------------------------


def test_run_and_summarize_no_provider_polls_to_completion(safety: SafetyChecker) -> None:
    result = run_and_summarize(
        ["/bin/echo", "hello"],
        safety=safety,
        runner=_mock_runner(),
        _poll_interval=0,
    )
    assert result.status == "completed"
    assert result.ok is True


def test_run_and_summarize_safety_block_returns_immediately(tmp_path: Path) -> None:
    cfg = _config(allowed_roots=[str(tmp_path)])
    kn = _knowledge()
    safety = SafetyChecker(cfg, kn)
    mock = _mock_runner()

    result = run_and_summarize(
        ["/bin/cat", "/etc/passwd"],
        safety=safety,
        runner=mock,
        _poll_interval=0,
    )
    assert result.status == "failed"
    assert result.error is not None
    mock.submit.assert_not_called()


# ---------------------------------------------------------------------------
# run_and_summarize — with provider
# ---------------------------------------------------------------------------


def test_run_and_summarize_with_provider_sets_summary(safety: SafetyChecker) -> None:
    provider = _make_provider("AI summary")
    result = run_and_summarize(
        ["/bin/echo", "output"],
        safety=safety,
        runner=_mock_runner(),
        provider=provider,
        _poll_interval=0,
    )
    assert result.status == "completed"
    assert result.summary == "AI summary"
    provider.complete.assert_called_once()


def test_run_and_summarize_provider_error_returns_original(safety: SafetyChecker) -> None:
    provider = MagicMock()
    provider.complete.side_effect = ProviderAuthError("bad key")

    result = run_and_summarize(
        ["/bin/echo", "hi"],
        safety=safety,
        runner=_mock_runner(),
        provider=provider,
        _poll_interval=0,
    )
    assert result.status == "completed"
    assert result.summary != "bad key"


def test_run_and_summarize_provider_receives_argv_in_message(safety: SafetyChecker) -> None:
    provider = _make_provider("ok")
    run_and_summarize(
        ["/bin/echo", "marker-xyz"],
        safety=safety,
        runner=_mock_runner(),
        provider=provider,
        _poll_interval=0,
    )
    call_messages = provider.complete.call_args[0][0]
    assert any("marker-xyz" in m["content"] for m in call_messages)
