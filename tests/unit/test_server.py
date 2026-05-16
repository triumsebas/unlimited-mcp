"""Unit tests for server.py — :func:`make_server` and the registered MCP tools.

:func:`make_server` is called with a tmp_path-backed config/jobs directory so
real subprocesses can be spawned without touching ~/.config.  The MCP layer is
exercised through :meth:`FastMCP.call_tool` (no network / stdio needed).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from unlimited_mcp.server import make_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_server(tmp_path: Path, *, config_yaml: str = "allowed_roots: []\n") -> Any:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_yaml)
    return make_server(
        cfg_file,
        knowledge_repo=tmp_path / "knowledge.yaml",  # missing → empty
        knowledge_local=tmp_path / "knowledge.local.yaml",  # missing → empty
        jobs_path=tmp_path / "jobs",
    )


def _result_dict(call_result: tuple[Any, Any]) -> dict[str, Any]:
    """Extract the JobResult fields from a FastMCP call_tool response."""
    _content, structured = call_result
    if structured:
        return structured  # type: ignore[return-value]
    # Fall back to parsing the text content
    text = _content[0].text if _content else "{}"
    return json.loads(text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


_PHASE_1_TOOLS = {
    # execution
    "run_command",
    "run_shell",
    "run_and_summarize",
    "delegate_to_agent",
    "submit_task",
    # job management
    "get_job_status",
    "get_job_result",
    "list_jobs",
    "cancel_job",
    "cleanup_jobs",
    "cleanup_branches",
    "cleanup_state",
    "cleanup_remote",
    # worker clarification
    "get_worker_questions",
    "answer_worker_questions",
    "resume_agent_task",
    # config / observability
    "list_capabilities",
    "query_logs",
    "add_provider",
    "add_agent",
    "configure_agent",
    "configure_safety",
    "add_host",
    "add_queue",
    "ssh_trust_host",
    "remove_entry",
    "list_safety_policy",
    "add_allowed_root",
    "remove_allowed_root",
    "add_deny_path",
    "remove_deny_path",
    # knowledge
    "lookup_agent_cli",
    "register_agent_knowledge",
    # meta
    "restart_server",
    "install_and_restart",
}


def test_make_server_registers_phase_1_tools(tmp_path: Path) -> None:
    app = _make_server(tmp_path)
    tools = _run(app.list_tools())
    names = {t.name for t in tools}
    assert names == _PHASE_1_TOOLS, f"diff: {names.symmetric_difference(_PHASE_1_TOOLS)}"


def test_tool_descriptions_are_non_empty(tmp_path: Path) -> None:
    app = _make_server(tmp_path)
    tools = _run(app.list_tools())
    for tool in tools:
        assert tool.description, f"Tool {tool.name!r} has no description"


# ---------------------------------------------------------------------------
# run_command via MCP layer
# ---------------------------------------------------------------------------


def test_run_command_tool_empty_argv_returns_failed(tmp_path: Path) -> None:
    app = _make_server(tmp_path, config_yaml=f"allowed_roots:\n  - {tmp_path}\n")
    result = _result_dict(_run(app.call_tool("run_command", {"argv": []})))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "SHELL_LIKE_BLOCKED"


def test_run_command_tool_out_of_root_returns_failed(tmp_path: Path) -> None:
    app = _make_server(tmp_path, config_yaml=f"allowed_roots:\n  - {tmp_path}\n")
    result = _result_dict(_run(app.call_tool("run_command", {"argv": ["/bin/cat", "/etc/passwd"]})))
    assert result["status"] == "failed"
    assert result["error"]["code"] == "OUT_OF_ROOT"


def test_run_command_tool_allowed_returns_running(tmp_path: Path) -> None:
    app = _make_server(tmp_path)
    result = _result_dict(_run(app.call_tool("run_command", {"argv": ["/bin/echo", "hi"]})))
    assert result["status"] == "running"
    assert result["job_id"]


def test_run_command_tool_confirm_flow(tmp_path: Path) -> None:
    cfg = f"allowed_roots:\n  - {tmp_path}\n"
    knowledge_yaml = "tools:\n  echo:\n    safety_class: dangerous\n"
    (tmp_path / "knowledge.yaml").write_text(knowledge_yaml)
    app2 = _make_server(tmp_path, config_yaml=cfg)

    first = _result_dict(_run(app2.call_tool("run_command", {"argv": ["/bin/echo", "hi"]})))
    assert first["status"] == "pending_confirmation"
    token = first["confirm_token"]
    assert token

    second = _result_dict(
        _run(app2.call_tool("run_command", {"argv": ["/bin/echo", "hi"], "confirm_token": token}))
    )
    assert second["status"] == "running"


# ---------------------------------------------------------------------------
# delegate_to_agent via MCP layer
# ---------------------------------------------------------------------------


def test_delegate_to_agent_unknown_raises_tool_error(tmp_path: Path) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    app = _make_server(tmp_path)
    with pytest.raises(ToolError):
        _run(app.call_tool("delegate_to_agent", {"agent_name": "nonexistent"}))


def test_delegate_to_agent_default_queue_does_not_shadow_agent_exec_host(
    tmp_path: Path,
) -> None:
    """Regression: queue='local' (default) must not set runner_override to a LocalRunner.

    If it did, the agent's own exec_host field would be silently ignored even when
    the caller omits both exec_host and queue arguments.
    """
    from unittest.mock import MagicMock, patch

    config_yaml = """\
allowed_roots: []
agents:
  remote_echo:
    cli: echo_cli
    exec_host: some_remote_host
"""
    app = _make_server(tmp_path, config_yaml=config_yaml)

    captured: dict = {}

    def fake_delegate(agent_name, *, runner_override=None, exec_host_override=None, **kw):
        captured["runner_override"] = runner_override
        captured["exec_host_override"] = exec_host_override
        # Return a minimal job-like dict so the tool doesn't crash
        return {
            "ok": True,
            "job_id": "test-job",
            "status": "completed",
            "summary": "ok",
            "diff_ref": None,
            "branch": None,
            "raw_output_ref": None,
            "error": None,
            "confirm_token": None,
            "risk_level": "low",
        }

    with patch("unlimited_mcp.server._delegate_to_agent", side_effect=fake_delegate):
        try:
            _run(
                app.call_tool(
                    "delegate_to_agent",
                    {"agent_name": "remote_echo", "prompt": "hello"},
                )
            )
        except Exception:
            pass  # agent not fully configured — we only care about captured values

    # The fix: runner_override must be None when queue=='local' so agent's exec_host wins
    assert captured.get("runner_override") is None, (
        f"runner_override should be None when queue='local', got {captured.get('runner_override')!r}"
    )


# ---------------------------------------------------------------------------
# run_and_summarize via MCP layer
# ---------------------------------------------------------------------------


def test_run_and_summarize_no_provider_completes(tmp_path: Path) -> None:
    app = _make_server(tmp_path)
    result = _result_dict(
        _run(app.call_tool("run_and_summarize", {"argv": ["/bin/echo", "hello"]}))
    )
    assert result["status"] == "completed"
    assert result["ok"] is True


def test_run_and_summarize_safety_block_returns_failed(tmp_path: Path) -> None:
    app = _make_server(tmp_path, config_yaml=f"allowed_roots:\n  - {tmp_path}\n")
    result = _result_dict(
        _run(app.call_tool("run_and_summarize", {"argv": ["/bin/cat", "/etc/passwd"]}))
    )
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_no_args_returns_nonzero() -> None:
    from unlimited_mcp.cli import main

    assert main([]) != 0


def test_cli_help_returns_zero() -> None:
    from unlimited_mcp.cli import main

    assert main(["--help"]) == 0


def test_cli_serve_help_returns_zero() -> None:
    from unlimited_mcp.cli import main

    assert main(["serve", "--help"]) == 0


def test_cli_unknown_subcommand_returns_nonzero() -> None:
    from unlimited_mcp.cli import main

    assert main(["frobulate"]) != 0
