"""Unit tests for :class:`unlimited_mcp.workspace.manager.WorkspaceManager`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from unlimited_mcp.config.schema import Knowledge, WorkspaceSpec
from unlimited_mcp.workspace import WorkspaceManager


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


def test_current_mode_returns_source(tmp_path: Path) -> None:
    src = tmp_path / "code"
    src.mkdir()
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    ws = mgr.create("read_only", source=src)
    assert ws.path == src
    assert ws.branch is None
    ws.cleanup()  # no-op


def test_none_mode_no_workspace(tmp_path: Path) -> None:
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    ws = mgr.create("sysops_local")
    assert ws.spec.mode == "none"
    ws.cleanup()  # no-op


def test_explicit_workspace_spec_used(tmp_path: Path) -> None:
    explicit = WorkspaceSpec(mode="current", result="apply_direct", dirty="refuse")
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    ws = mgr.create(explicit, source=tmp_path)
    assert ws.spec is explicit


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_git_worktree_mode_creates_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")

    ws = mgr.create("safe_dev", source=repo, label="docstrings")
    try:
        assert ws.spec.mode == "git_worktree"
        assert ws.spec.result == "leave_branch"
        assert ws.path.exists()
        assert ws.branch and ws.branch.startswith("unlimited-mcp/docstrings")
        # The worktree carries the repo's content.
        assert (ws.path / "README.md").read_text(encoding="utf-8") == "hi\n"
    finally:
        ws.cleanup()
        assert not ws.path.exists()


def test_git_worktree_without_source_raises(tmp_path: Path) -> None:
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    with pytest.raises(ValueError, match="git_worktree mode requires"):
        mgr.create("safe_dev", source=None)


def test_temp_copy_mode_creates_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a", encoding="utf-8")

    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    ws = mgr.create(
        WorkspaceSpec(mode="temp_copy", result="report_only", dirty=None),
        source=src,
    )
    try:
        assert ws.path.exists()
        assert (ws.path / "a.py").read_text(encoding="utf-8") == "a"
    finally:
        ws.cleanup()
        assert not ws.path.exists()


def test_temp_copy_without_source_raises(tmp_path: Path) -> None:
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    spec = WorkspaceSpec(mode="temp_copy", result="report_only", dirty=None)
    with pytest.raises(ValueError, match="temp_copy mode requires"):
        mgr.create(spec, source=None)


def test_remote_cwd_raises_not_implemented(tmp_path: Path) -> None:
    mgr = WorkspaceManager(Knowledge(), tmp_path / "ws")
    with pytest.raises(NotImplementedError, match="phase 3"):
        mgr.create("sysops_remote")
