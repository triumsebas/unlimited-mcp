# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Tier A — headless regression smoke suite.

Run this on **every change**.  It drives the server exclusively through
``app.call_tool`` (the real MCP path) using only ``echo``/``sleep`` — no
API keys, no network, no real coding agent.  It completes in seconds.

The goal is *capability coverage*: one focused test per MCP capability
category so that any future change that breaks a subsystem fails here
with a diagnostic that says **which** subsystem broke.

Two tests are explicit regression pins for bugs already shipped:

* ``test_summary_is_populated_on_success`` — the runner used to return a
  hard-coded ``"Completed successfully."`` discarding all agent output.
* ``test_delegate_ts_queue_accepts_job_id`` — ``TsRunner.submit`` was
  missing the ``job_id`` kwarg ``LocalRunner`` had, so every
  ``delegate_to_agent(queue="ts")`` raised ``TypeError``.

The live end-to-end suite (real agents, remote SSH, smolagents, clarify)
is **Tier B** — see the "Regression suite" section in ``AGENTS.md``.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from unlimited_mcp.server import make_server


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _call(app: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call *tool* through the MCP path and return the structured dict."""
    _content, structured = _run(app.call_tool(tool, args))
    return structured or {}


def _poll(app: Any, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    """Poll get_job_result until the job leaves 'running'."""
    deadline = time.monotonic() + timeout
    r: dict[str, Any] = {}
    while True:
        r = _call(app, "get_job_result", {"job_id": job_id})
        if r["status"] != "running":
            return r
        assert time.monotonic() < deadline, f"job {job_id} did not finish in {timeout}s"
        time.sleep(0.05)


@pytest.fixture
def app(tmp_path: Path) -> Any:
    """A server wired with an echo agent and tmp_path allow-listed."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"schema_version: 1\n"
        f"allowed_roots:\n  - {tmp_path}\n"
        f"agents:\n  echo_agent:\n    cli: echo\n",
    )
    knowledge_file = tmp_path / "knowledge.yaml"
    knowledge_file.write_text(
        "clis:\n"
        "  echo:\n"
        "    command_template: /bin/echo {prompt}\n"
        "tools:\n"
        "  sleep:\n"
        "    safety_class: read\n",
    )
    return make_server(
        cfg_file,
        knowledge_repo=knowledge_file,
        knowledge_local=tmp_path / "knowledge.local.yaml",
        jobs_path=tmp_path / "jobs",
    )


# ---------------------------------------------------------------------------
# Tool surface — accidental removal/rename of any tool fails here
# ---------------------------------------------------------------------------


def test_tool_surface_intact(app: Any) -> None:
    expected = {
        "run_command",
        "run_shell",
        "run_and_summarize",
        "delegate_to_agent",
        "submit_task",
        "get_job_status",
        "get_job_result",
        "list_jobs",
        "cancel_job",
        "detect_conflicts",
        "cleanup_jobs",
        "cleanup_branches",
        "cleanup_state",
        "get_worker_questions",
        "answer_worker_questions",
        "resume_agent_task",
        "list_capabilities",
        "query_logs",
        "add_provider",
        "add_agent",
        "configure_agent",
        "remove_entry",
        "list_safety_policy",
        "add_allowed_root",
        "remove_allowed_root",
        "add_deny_path",
        "remove_deny_path",
        "lookup_agent_cli",
        "register_agent_knowledge",
        "restart_server",
        "install_and_restart",
    }
    tools = _run(app.list_tools())
    names = {t.name for t in tools}
    missing = expected - names
    assert not missing, f"tools disappeared from the surface: {missing}"
    for t in tools:
        assert t.description, f"tool {t.name!r} lost its description"


# ---------------------------------------------------------------------------
# Config CRUD — add → observe in list_capabilities → remove → gone
# ---------------------------------------------------------------------------


def test_config_crud_roundtrip(app: Any) -> None:
    _call(
        app,
        "add_provider",
        {
            "name": "smoke_prov",
            "provider_type": "openai_compat",
            "model": "x",
            "base_url": "http://localhost:1",
        },
    )
    caps = _call(app, "list_capabilities", {})
    assert "smoke_prov" in caps["providers"], caps["providers"]

    _call(app, "remove_entry", {"section": "providers", "name": "smoke_prov"})
    caps = _call(app, "list_capabilities", {})
    assert "smoke_prov" not in caps["providers"]

    # allowed_roots add/remove roundtrip
    _call(app, "add_allowed_root", {"path": "/tmp/smoke-root"})
    caps = _call(app, "list_capabilities", {})
    assert any("smoke-root" in p for p in caps["allowed_roots"])
    _call(app, "remove_allowed_root", {"path": "/tmp/smoke-root"})
    caps = _call(app, "list_capabilities", {})
    assert not any("smoke-root" in p for p in caps["allowed_roots"])


# ---------------------------------------------------------------------------
# Sysops local
# ---------------------------------------------------------------------------


def test_run_command_local(app: Any) -> None:
    r = _call(app, "run_command", {"argv": ["/bin/echo", "smoke-hello"]})
    assert r["status"] == "running", r
    done = _poll(app, r["job_id"])
    assert done["status"] == "completed", done
    assert done["ok"] is True


def test_run_shell_pipe(app: Any) -> None:
    r = _call(
        app,
        "run_shell",
        {
            "script": "echo one two three | tr ' ' '\\n' | wc -l",
            "i_understand_this_runs_a_shell_script": True,
        },
    )
    done = _poll(app, r["job_id"]) if r["status"] == "running" else r
    assert done["status"] == "completed", done


# ---------------------------------------------------------------------------
# REGRESSION PIN 1 — summary must carry real output, not a hard-coded string
# ---------------------------------------------------------------------------


def test_summary_is_populated_on_success(app: Any) -> None:
    r = _call(app, "run_command", {"argv": ["/bin/echo", "REGRESSION-MARKER-XYZ"]})
    done = _poll(app, r["job_id"])
    assert done["status"] == "completed", done
    assert "REGRESSION-MARKER-XYZ" in done["summary"], (
        "summary lost real output — the 'Completed successfully.' "
        f"regression is back: {done['summary']!r}"
    )


# ---------------------------------------------------------------------------
# Safety pipeline — allowlist, shell-block, confirmation flow
# ---------------------------------------------------------------------------


def test_safety_out_of_root(app: Any) -> None:
    blocked = _call(app, "run_command", {"argv": ["/bin/cat", "/etc/passwd"]})
    assert blocked["status"] == "failed", blocked
    assert blocked["error"]["code"] == "OUT_OF_ROOT", blocked


def test_safety_shell_like_blocked(app: Any) -> None:
    empty = _call(app, "run_command", {"argv": []})
    assert empty["status"] == "failed"
    assert empty["error"]["code"] == "SHELL_LIKE_BLOCKED", empty


def test_safety_confirmation_flow(tmp_path: Path) -> None:
    # Separate server: echo classified 'dangerous' to trip the gate.
    (tmp_path / "config.yaml").write_text(
        f"schema_version: 1\nallowed_roots:\n  - {tmp_path}\n",
    )
    kf = tmp_path / "knowledge.yaml"
    kf.write_text(
        "clis:\n  echo:\n    command_template: /bin/echo {prompt}\n"
        "tools:\n  echo:\n    safety_class: dangerous\n",
    )
    app = make_server(
        tmp_path / "config.yaml",
        knowledge_repo=kf,
        knowledge_local=tmp_path / "knowledge.local.yaml",
        jobs_path=tmp_path / "jobs",
    )
    first = _call(app, "run_command", {"argv": ["/bin/echo", "hi"]})
    assert first["status"] == "pending_confirmation", first
    token = first["confirm_token"]
    assert token

    ok = _call(app, "run_command", {"argv": ["/bin/echo", "hi"], "confirm_token": token})
    assert ok["status"] == "running", ok

    reused = _call(app, "run_command", {"argv": ["/bin/echo", "hi"], "confirm_token": token})
    assert reused["error"]["code"] == "CONFIRMATION_EXPIRED", reused


# ---------------------------------------------------------------------------
# Job lifecycle — inbox, idempotency, cancel
# ---------------------------------------------------------------------------


def test_submit_task_and_idempotency(app: Any) -> None:
    j1 = _call(
        app,
        "submit_task",
        {
            "argv": ["/bin/echo", "task-a"],
            "tag": "smoke",
            "idempotency_key": "smoke-key-1",
        },
    )
    assert j1["status"] in ("running", "completed"), j1

    # Same idempotency key → same job, no second submission.
    j2 = _call(
        app,
        "submit_task",
        {
            "argv": ["/bin/echo", "task-a"],
            "tag": "smoke",
            "idempotency_key": "smoke-key-1",
        },
    )
    assert j2["job_id"] == j1["job_id"], (j1, j2)

    jobs = _call(app, "list_jobs", {"tag": "smoke"})
    assert isinstance(jobs, (list, dict))


def test_cancel_job(app: Any) -> None:
    sleeping = _call(app, "run_command", {"argv": ["/bin/sleep", "120"]})
    assert sleeping["status"] == "running", sleeping
    sid = sleeping["job_id"]

    cancelled = _call(app, "cancel_job", {"job_id": sid})
    assert cancelled["status"] == "cancelled", cancelled

    again = _call(app, "cancel_job", {"job_id": sid})
    assert again["status"] == "cancelled"

    missing = _call(app, "cancel_job", {"job_id": "no-such-job"})
    assert missing["error"]["code"] == "JOB_NOT_FOUND", missing


# ---------------------------------------------------------------------------
# Agent delegation — local queue
# ---------------------------------------------------------------------------


def test_delegate_local_queue(app: Any) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _run(app.call_tool("delegate_to_agent", {"agent_name": "no_such_agent"}))

    d = _call(app, "delegate_to_agent", {"agent_name": "echo_agent", "prompt": "marker"})
    assert d["status"] == "running", d
    done = _poll(app, d["job_id"])
    assert done["status"] == "completed", done


# ---------------------------------------------------------------------------
# REGRESSION PIN 2 — TsRunner.submit must accept job_id (was a TypeError)
# ---------------------------------------------------------------------------


@pytest.mark.requires_ts
@pytest.mark.skipif(shutil.which("ts") is None, reason="task-spooler not on PATH")
def test_delegate_ts_queue_accepts_job_id(app: Any) -> None:
    d = _call(
        app,
        "delegate_to_agent",
        {
            "agent_name": "echo_agent",
            "prompt": "ts-marker",
            "queue": "ts",
        },
    )
    assert d.get("error", None) is None, (
        f"ts queue regressed — TsRunner.submit rejected job_id again: {d}"
    )
    assert d["status"] in ("running", "completed"), d
    done = _poll(app, d["job_id"], timeout=15.0)
    assert done["status"] == "completed", done


# ---------------------------------------------------------------------------
# Cleanup + observability
# ---------------------------------------------------------------------------


def test_cleanup_and_query_logs(app: Any) -> None:
    r = _call(app, "run_command", {"argv": ["/bin/echo", "log-event"]})
    _poll(app, r["job_id"])

    logs = _call(app, "query_logs", {})
    assert isinstance(logs, (list, dict)), logs

    cleaned = _call(app, "cleanup_jobs", {})
    assert isinstance(cleaned, (list, dict)), cleaned


# ---------------------------------------------------------------------------
# Post-job: changed_files + quality gate (Fase 0/1)
# ---------------------------------------------------------------------------


def _git_repo(root: Path) -> str:
    """Init a git repo with one committed file; return the base commit SHA."""
    import subprocess as sp

    sp.run(["git", "-C", str(root), "init", "-q"], check=True)
    sp.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    sp.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "pyproject.toml").write_text("[tool.ruff]\n")
    (root / "a.py").write_text("x = 1\n")
    sp.run(["git", "-C", str(root), "add", "-A"], check=True)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return sp.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_changed_files_includes_committed_and_untracked(tmp_path: Path) -> None:
    from unlimited_mcp.workspace.git_worktree import changed_files

    base = _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")  # modified, uncommitted
    (tmp_path / "new.py").write_text("y = 3\n")  # new, untracked
    cf = changed_files(str(tmp_path), base)
    assert "a.py" in cf and "new.py" in cf, cf


def test_quality_gate_nopass_on_unused_import(tmp_path: Path) -> None:
    if shutil.which("ruff") is None:
        pytest.skip("ruff not installed")
    from unlimited_mcp.quality.gate import run_quality_gate

    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("import os\nx = 1\n")
    res = run_quality_gate(str(tmp_path), ["a.py"], report_dir=tmp_path / "_r")
    assert res.status == "NOPASS", res
    assert any(i.rule == "F401" for i in res.issues), res.issues
    assert res.report_ref is not None


def test_quality_gate_pass_and_autofix(tmp_path: Path) -> None:
    if shutil.which("ruff") is None:
        pytest.skip("ruff not installed")
    from unlimited_mcp.quality.gate import run_quality_gate

    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n")  # bad formatting, no lint error
    res = run_quality_gate(str(tmp_path), ["a.py"])
    assert res.status == "PASS", res
    assert "a.py" in res.auto_fixed, res.auto_fixed
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_quality_gate_notdetected_for_unknown_language(tmp_path: Path) -> None:
    from unlimited_mcp.quality.gate import run_quality_gate

    res = run_quality_gate(str(tmp_path), ["README.md", "data.csv"])
    assert res.status == "NOTDETECTED", res


def test_quality_gate_missingdep(tmp_path: Path, monkeypatch: Any) -> None:
    from unlimited_mcp.quality import gate

    _git_repo(tmp_path)
    (tmp_path / "main.go").write_text("package main\n")
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    res = gate.run_quality_gate(str(tmp_path), ["main.go"])
    assert res.status == "MISSINGDEP", res
    assert res.missing_deps, res


# ---------------------------------------------------------------------------
# Conflict detection (Fase 2)
# ---------------------------------------------------------------------------


def test_detect_conflicts_reports_overlap(app: Any, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from unlimited_mcp.jobs.result import JobResult
    from unlimited_mcp.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    now = datetime.now(UTC)
    for jid, files in (("job-a", ["src/x.py", "src/y.py"]), ("job-b", ["src/y.py"])):
        store.write_result(
            JobResult(
                ok=True,
                job_id=jid,
                status="completed",
                tool="delegate_to_agent",
                started_at=now,
                finished_at=now,
                changed_files=files,
            )
        )
    out = _call(app, "detect_conflicts", {})
    pairs = {tuple(sorted(c["jobs"])): c["files"] for c in out["conflicts"]}
    assert ("job-a", "job-b") in pairs, out
    assert pairs[("job-a", "job-b")] == ["src/y.py"], out


def test_detect_conflicts_empty_when_disjoint(app: Any, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from unlimited_mcp.jobs.result import JobResult
    from unlimited_mcp.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    now = datetime.now(UTC)
    for jid, files in (("job-c", ["a.py"]), ("job-d", ["b.py"])):
        store.write_result(
            JobResult(
                ok=True,
                job_id=jid,
                status="completed",
                tool="delegate_to_agent",
                started_at=now,
                finished_at=now,
                changed_files=files,
            )
        )
    out = _call(app, "detect_conflicts", {})
    assert out["conflicts"] == [], out


# ---------------------------------------------------------------------------
# Auto-retry on transient ts enqueue failure (Fase 3)
# ---------------------------------------------------------------------------


def test_ts_submit_retries_then_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from unlimited_mcp.jobs import runner_ts
    from unlimited_mcp.jobs.store import JobStore

    monkeypatch.setattr(runner_ts.time, "sleep", lambda _s: None)
    state = {"enqueue_calls": 0}

    def fake_run(argv: list[str], *a: Any, **k: Any) -> Any:
        if len(argv) >= 2 and argv[1] == "-V":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        state["enqueue_calls"] += 1
        if state["enqueue_calls"] < 3:
            return SimpleNamespace(returncode=1, stdout="", stderr="socket busy")
        return SimpleNamespace(returncode=0, stdout="42\n", stderr="")

    monkeypatch.setattr(runner_ts.subprocess, "run", fake_run)
    r = runner_ts.TsRunner(JobStore(tmp_path / "jobs"), ts_bin="ts")
    res = r.submit(["/bin/echo", "x"], tool="run_command")
    assert state["enqueue_calls"] == 3, state
    assert res.status == "running" and res.error is None, res


def test_ts_submit_fails_after_max_retries(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from unlimited_mcp.jobs import runner_ts
    from unlimited_mcp.jobs.store import JobStore

    monkeypatch.setattr(runner_ts.time, "sleep", lambda _s: None)
    state = {"enqueue_calls": 0}

    def fake_run(argv: list[str], *a: Any, **k: Any) -> Any:
        if len(argv) >= 2 and argv[1] == "-V":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        state["enqueue_calls"] += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="daemon down")

    monkeypatch.setattr(runner_ts.subprocess, "run", fake_run)
    r = runner_ts.TsRunner(JobStore(tmp_path / "jobs"), ts_bin="ts")
    res = r.submit(["/bin/echo", "x"], tool="run_command")
    assert state["enqueue_calls"] == runner_ts._RETRY_MAX_ATTEMPTS, state
    assert res.status == "failed" and res.error.code == "TS_SUBMIT_FAILED", res
    assert res.error.retryable is True, res
