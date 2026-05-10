"""Unit tests for :mod:`unlimited_mcp.workspace.git_worktree`.

These tests shell out to real ``git``. They are still considered "unit"
tests (no external services, deterministic, fast) but they require the
``git`` binary on PATH — universal on dev machines. If git is missing
the whole module is skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from unlimited_mcp.workspace.git_worktree import (
    cleanup_git_worktree,
    create_git_worktree,
    is_git_repo,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _init_repo(path: Path) -> Path:
    """Create a tiny git repo at ``path`` with one commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)
    return path


def test_is_git_repo_true_for_real_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    assert is_git_repo(repo) is True


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_git_repo(plain) is False


def test_is_git_repo_false_for_missing_path(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path / "ghost") is False


def test_create_and_cleanup_roundtrip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "wt" / "feat-1"

    handle = create_git_worktree(repo, "feat-1", target)
    assert handle.path == target
    assert handle.branch == "feat-1"
    assert target.exists()
    assert (target / "README.md").read_text(encoding="utf-8") == "hello\n"

    cleanup_git_worktree(handle, delete_branch=True)
    assert not target.exists()


def test_diff_returns_empty_for_clean_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "wt" / "feat-2"
    handle = create_git_worktree(repo, "feat-2", target)
    try:
        assert handle.diff() == ""
    finally:
        cleanup_git_worktree(handle, delete_branch=True)


def test_diff_returns_changes_after_edit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "wt" / "feat-3"
    handle = create_git_worktree(repo, "feat-3", target)
    try:
        (target / "README.md").write_text("hello\nworld\n", encoding="utf-8")
        diff = handle.diff()
        assert "+world" in diff
        assert "README.md" in diff
    finally:
        cleanup_git_worktree(handle, delete_branch=True)


def test_create_on_non_repo_raises_value_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        create_git_worktree(plain, "feat-x", tmp_path / "wt")


def test_create_with_existing_target_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "wt"
    target.mkdir()
    with pytest.raises(FileExistsError):
        create_git_worktree(repo, "feat-y", target)


def test_cleanup_idempotent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "wt" / "feat-z"
    handle = create_git_worktree(repo, "feat-z", target)
    cleanup_git_worktree(handle, delete_branch=True)
    # Second call is a no-op rather than raising.
    cleanup_git_worktree(handle, delete_branch=True)
