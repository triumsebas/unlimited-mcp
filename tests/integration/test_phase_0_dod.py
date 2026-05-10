"""Phase 0 — Definition of Done.

This is the single integration test the plan calls out as the gate
between Phase 0 and Phase 1: when it passes, the substrate is proven
to compose end-to-end and we can start wiring MCP tools on top of it.

The test exercises every Phase 0 module in a realistic flow:

    ConfigStore.load
        → SafetyChecker.classify (allowed-roots, classes, confirmation)
            → WorkspaceManager.create(git_worktree)
                → JobStore.write(JobResult) → read back
        → ConfigStore.update (atomic, comments preserved)
            → reload — observe the mutation
        → observability.configure_logging — events land in JSONL

If any module's contract drifts, this test fails. It is deliberately
opinionated about the integration *shape* — adding new modules later
should extend it rather than work around it.
"""

from __future__ import annotations

import json
import logging as stdlib_logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.jobs.result import (
    CommandRecord,
    JobResult,
)
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.observability.logging import configure_logging, get_logger
from unlimited_mcp.safety.argv_check import SafetyChecker
from unlimited_mcp.workspace import WorkspaceManager

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "primes.py").write_text("def is_prime(n):\n    return n > 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "primes.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)
    return path


def test_phase_0_definition_of_done(tmp_path: Path) -> None:
    """The full Phase 0 substrate composes from one orchestrator-shaped flow."""

    # ---- 0. Layout: pretend `tmp_path` is the runtime root ---------------
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    log_dir = state_dir / "logs"
    jobs_root = state_dir / "jobs"
    workspaces_root = state_dir / "workspaces"
    jobs_root.mkdir()
    workspaces_root.mkdir()

    # The user's target repo is what aider/opencode would edit.
    target_repo = _init_git_repo(tmp_path / "target_repo")

    # ---- 1. Logging: events are JSONL, file-only -------------------------
    log_path = configure_logging(log_dir)
    log = get_logger("phase0").bind(tool="phase_0_dod")
    log.info("starting")

    # ---- 2. ConfigStore: write a config file with a comment, then read --
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "# default-deny posture: only the runtime tmp dir is allowed\n"
        "schema_version: 1\n"
        "allowed_roots:\n"
        f"  - {tmp_path}\n"
        "safety:\n"
        "  allow_shell_like_argv: false\n"
        "agents:\n"
        "  # aider configured against opencode go\n"
        "  aider_local:\n"
        "    cli: aider\n"
        "    cost_tier: 1\n"
        "    params:\n"
        "      git: false\n"
        "      model: openai/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    cfg_store = ConfigStore(config_path)
    cfg = cfg_store.get()
    assert "aider_local" in cfg.agents
    assert cfg.agents["aider_local"].params["git"] is False

    # ---- 3. KnowledgeStore: tools catalog with safety classes -----------
    repo_knowledge = config_dir / "knowledge.yaml"
    repo_knowledge.write_text(
        "tools:\n"
        "  rg:\n"
        "    safety_class: read\n"
        "  rm:\n"
        "    safety_class: mutating\n"
        "    flag_patterns:\n"
        "      - { match: ['-rf'], escalates_to: dangerous }\n"
        "shell_like_argv:\n"
        "  bash:\n"
        "    inline_flags: ['-c', '-lc']\n",
        encoding="utf-8",
    )
    kn_store = KnowledgeStore(repo_knowledge, config_dir / "knowledge.local.yaml")
    knowledge = kn_store.get()
    assert "rg" in knowledge.tools

    # ---- 4. SafetyChecker: a read inside the root passes ----------------
    checker = SafetyChecker(cfg, knowledge)
    target_file = target_repo / "primes.py"
    decision = checker.check_run_command(["rg", "is_prime", str(target_file)])
    assert decision.allowed is True
    assert decision.safety_class == "read"
    assert decision.risk_level == "low"

    # ...and a dangerous one trips the confirmation gate.
    danger = checker.check_run_command(["rm", "-rf", str(target_repo / "scratch")])
    assert danger.allowed is False
    assert danger.requires_confirmation is True
    assert danger.confirm_token

    # ---- 5. WorkspaceManager: spin up a git worktree --------------------
    wm = WorkspaceManager(knowledge, base_dir=workspaces_root)
    workspace = wm.create("safe_dev", source=target_repo, label="add-docstring")
    try:
        assert workspace.spec.mode == "git_worktree"
        assert workspace.path.exists()
        assert workspace.branch == "add-docstring"
        # The worker would now operate inside workspace.path. Simulate an edit.
        edited = workspace.path / "primes.py"
        edited.write_text(
            '"""Prime helpers."""\n\ndef is_prime(n):\n    return n > 1\n',
            encoding="utf-8",
        )

        # ---- 6. JobStore: write a JobResult referencing the worktree ----
        store = JobStore(jobs_root)
        job_id = JobStore.make_job_id("delegate_to_agent")
        now = datetime.now(UTC)
        result = JobResult(
            ok=True,
            job_id=job_id,
            status="completed",
            tool="delegate_to_agent",
            started_at=now,
            finished_at=now,
            duration_ms=1234,
            summary="Added a module docstring to primes.py",
            changed_files=["primes.py"],
            branch=workspace.branch,
            worktree_path=str(workspace.path),
            commands_run=[
                CommandRecord(
                    argv=["aider", "--no-git", "--message", "<redacted prompt>"],
                    cwd=str(workspace.path),
                    exit_code=0,
                    safety_class="mutating",
                    risk_level="low",
                )
            ],
        )
        store.write_result(result)

        # And the diff is captured for `result_mode=leave_branch`.
        from unlimited_mcp.workspace.git_worktree import GitWorktreeHandle

        diff = GitWorktreeHandle(
            repo=target_repo,
            path=workspace.path,
            branch=workspace.branch or "",
        ).diff()
        assert "Prime helpers" in diff

        # ---- 7. Read it back and assert the round-trip preserves data ---
        loaded = store.read_result(job_id)
        assert loaded is not None
        assert loaded.ok is True
        assert loaded.branch == "add-docstring"
        assert loaded.worktree_path == str(workspace.path)
        assert loaded.commands_run[0].safety_class == "mutating"
    finally:
        workspace.cleanup()
        assert not workspace.path.exists()

    # ---- 8. ConfigStore.update: atomic mutation preserves comments ------
    def enable_git(doc: CommentedMap) -> None:
        doc["agents"]["aider_local"]["params"]["git"] = True

    new_cfg = cfg_store.update(enable_git)
    assert new_cfg.agents["aider_local"].params["git"] is True

    text = config_path.read_text(encoding="utf-8")
    assert "# default-deny posture" in text  # top-level comment kept
    assert "# aider configured against opencode go" in text  # nested comment kept
    assert "git: true" in text

    # ---- 9. Reload: a fresh ConfigStore observes the mutation -----------
    fresh = ConfigStore(config_path).get()
    assert fresh.agents["aider_local"].params["git"] is True

    # ---- 10. Logging side-effect: the event we wrote is in the file ----
    for h in list(stdlib_logging.getLogger().handlers):
        h.flush()
    line = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert line["event"] == "starting"
    assert line["tool"] == "phase_0_dod"
    assert line["level"] == "info"
