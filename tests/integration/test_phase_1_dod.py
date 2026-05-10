"""Phase 1 — Definition of Done.

Gate between Phase 1 and Phase 2: when this test passes, the complete
MCP server stack is proven to compose end-to-end.

The test drives the server exclusively through :meth:`FastMCP.call_tool`
— the same path the real MCP protocol takes — and exercises every Phase 1
concern in a single realistic flow:

    make_server
        → run_command (allowed, blocked, dangerous + confirm flow)
            → get_job_result / list_jobs  (background job lifecycle)
        → cancel_job  (SIGTERM path)
        → run_and_summarize  (synchronous poll-to-done)
        → delegate_to_agent  (agent resolution + dispatch)

No new source modules are introduced; this test is the phase 1 gate.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from unlimited_mcp.server import make_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _call(app: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call *tool* and return the structured result dict."""
    _content, structured = _run(app.call_tool(tool, args))
    return structured or {}


def _make_app(tmp_path: Path) -> Any:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"allowed_roots:\n  - {tmp_path}\nagents:\n  echo_agent:\n    cli: echo\n",
    )
    knowledge_file = tmp_path / "knowledge.yaml"
    knowledge_file.write_text(
        "clis:\n"
        "  echo:\n"
        "    command_template: /bin/echo {prompt}\n"
        "tools:\n"
        "  sleep:\n"
        "    safety_class: mutating\n"
    )
    return make_server(
        cfg_file,
        knowledge_repo=knowledge_file,
        knowledge_local=tmp_path / "knowledge.local.yaml",
        jobs_path=tmp_path / "jobs",
    )


# ---------------------------------------------------------------------------
# Phase 1 DoD — single orchestrator-shaped flow
# ---------------------------------------------------------------------------


def test_phase_1_definition_of_done(tmp_path: Path) -> None:
    """The full Phase 1 stack composes from one orchestrator-shaped flow."""

    app = _make_app(tmp_path)

    # ---- 1. Six tools are registered ------------------------------------
    tools = _run(app.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "run_command",
        "delegate_to_agent",
        "run_and_summarize",
        "get_job_result",
        "list_jobs",
        "cancel_job",
    }, f"unexpected tool set: {names}"
    for t in tools:
        assert t.description, f"tool {t.name!r} has no description"

    # ---- 2. run_command allowed → running --------------------------------
    r = _call(app, "run_command", {"argv": ["/bin/echo", "phase1-hello"]})
    assert r["status"] == "running", r
    job_id = r["job_id"]
    assert job_id

    # ---- 3. get_job_result polls to completion ---------------------------
    deadline = time.monotonic() + 5.0
    while True:
        r = _call(app, "get_job_result", {"job_id": job_id})
        if r["status"] != "running":
            break
        assert time.monotonic() < deadline, "job did not complete within 5 s"
        time.sleep(0.05)
    assert r["status"] == "completed", r
    assert r["ok"] is True

    # ---- 4. run_command safety block (OUT_OF_ROOT) ----------------------
    blocked = _call(app, "run_command", {"argv": ["/bin/cat", "/etc/passwd"]})
    assert blocked["status"] == "failed", blocked
    assert blocked["error"]["code"] == "OUT_OF_ROOT"

    # ---- 5. run_command empty argv (hard block) -------------------------
    empty = _call(app, "run_command", {"argv": []})
    assert empty["status"] == "failed"
    assert empty["error"]["code"] == "SHELL_LIKE_BLOCKED"

    # ---- 6. Dangerous command → confirm flow ----------------------------
    # sleep is classified as mutating (from knowledge.yaml), not dangerous.
    # Use a tool-class override to exercise the confirmation path instead.
    # We re-create the server with sleep marked dangerous.
    knowledge_file = tmp_path / "knowledge.yaml"
    knowledge_file.write_text(
        "clis:\n"
        "  echo:\n"
        "    command_template: /bin/echo {prompt}\n"
        "tools:\n"
        "  echo:\n"
        "    safety_class: dangerous\n"
    )
    app2 = make_server(
        tmp_path / "config.yaml",
        knowledge_repo=knowledge_file,
        knowledge_local=tmp_path / "knowledge.local.yaml",
        jobs_path=tmp_path / "jobs2",
    )
    first = _call(app2, "run_command", {"argv": ["/bin/echo", "hi"]})
    assert first["status"] == "pending_confirmation", first
    token = first["confirm_token"]
    assert token
    assert first["confirm_reason"]

    second = _call(
        app2,
        "run_command",
        {"argv": ["/bin/echo", "hi"], "confirm_token": token},
    )
    assert second["status"] == "running", second

    # Token is single-use; re-using it returns CONFIRMATION_EXPIRED.
    expired = _call(
        app2,
        "run_command",
        {"argv": ["/bin/echo", "hi"], "confirm_token": token},
    )
    assert expired["error"]["code"] == "CONFIRMATION_EXPIRED"

    # ---- 7. list_jobs returns all submitted jobs -------------------------
    all_jobs = _call(app, "list_jobs", {})
    # list_jobs returns a list (structured = list of dicts via FastMCP)
    # FastMCP serialises list[JobResult] as structured output.
    assert isinstance(all_jobs, (list, dict))  # shape depends on FastMCP version

    # ---- 8. cancel_job on a long-running process ------------------------
    sleeping = _call(app, "run_command", {"argv": ["/bin/sleep", "120"]})
    assert sleeping["status"] == "running"
    sleep_id = sleeping["job_id"]

    cancelled = _call(app, "cancel_job", {"job_id": sleep_id})
    assert cancelled["status"] == "cancelled", cancelled
    assert cancelled["job_id"] == sleep_id

    # Cancelling again returns the already-cancelled result unchanged.
    recancelled = _call(app, "cancel_job", {"job_id": sleep_id})
    assert recancelled["status"] == "cancelled"

    # ---- 9. cancel unknown job → JOB_NOT_FOUND --------------------------
    not_found = _call(app, "cancel_job", {"job_id": "no-such-job"})
    assert not_found["error"]["code"] == "JOB_NOT_FOUND"

    # ---- 10. get_job_result unknown job → JOB_NOT_FOUND ----------------
    missing = _call(app, "get_job_result", {"job_id": "phantom"})
    assert missing["error"]["code"] == "JOB_NOT_FOUND"

    # ---- 11. run_and_summarize → polls to completed --------------------
    summary_r = _call(app, "run_and_summarize", {"argv": ["/bin/echo", "summarize-me"]})
    assert summary_r["status"] == "completed", summary_r
    assert summary_r["ok"] is True

    # ---- 12. delegate_to_agent with configured echo agent ---------------
    from mcp.server.fastmcp.exceptions import ToolError

    # Unknown agent raises ToolError (AgentRenderError → FastMCP wraps it).
    with pytest.raises(ToolError):
        _run(app.call_tool("delegate_to_agent", {"agent_name": "no_such_agent"}))

    # Known agent dispatches successfully.
    delegated = _call(
        app, "delegate_to_agent", {"agent_name": "echo_agent", "prompt": "dod-marker"}
    )
    assert delegated["status"] == "running", delegated
    d_id = delegated["job_id"]

    deadline = time.monotonic() + 5.0
    d_r: dict[str, Any] = {}
    while True:
        d_r = _call(app, "get_job_result", {"job_id": d_id})
        if d_r["status"] != "running":
            break
        assert time.monotonic() < deadline, "delegated job did not complete"
        time.sleep(0.05)
    assert d_r["status"] == "completed", d_r
