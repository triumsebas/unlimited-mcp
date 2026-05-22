# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Git worktree backend.

A git worktree is a separate working directory tied to the same
repository and pointing at a different branch. It's the safest place
for a delegated worker to write into a real repo: any mistake is
isolated, the user reviews via ``git diff`` or merges via ``git
merge``, and cleanup is one command.

We shell out to ``git`` rather than using a Python git library — every
worktree-capable git ships with the right commands, and we avoid a
heavy dependency.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitWorktreeHandle:
    """Reference to a created worktree. Immutable so callers can't
    accidentally rewrite the cleanup target."""

    repo: Path
    path: Path
    branch: str
    base_sha: str | None = None  # repo HEAD at creation — the branch's fork point

    def diff(self) -> str:
        """Return ``git diff`` output for this worktree (porcelain).

        Returns the empty string for a clean worktree.
        """
        result = subprocess.run(
            ["git", "-C", str(self.path), "diff"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout


def changed_files(worktree_path: str | Path, base_sha: str | None = None) -> list[str]:
    """Return repo-relative paths the worker changed in this worktree.

    Captures committed + staged + unstaged modifications to tracked files
    (``git diff --name-only`` against the worktree's base commit) plus new
    untracked files. When *base_sha* is unknown we fall back to ``HEAD``,
    which misses files the worker committed — pass *base_sha* whenever the
    branch's fork point is known.
    """
    wt = str(worktree_path)
    out: set[str] = set()

    diff_target = base_sha or "HEAD"
    tracked = subprocess.run(
        ["git", "-C", wt, "diff", "--name-only", diff_target],
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode == 0:
        out.update(line for line in tracked.stdout.splitlines() if line)

    untracked = subprocess.run(
        ["git", "-C", wt, "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=False,
    )
    if untracked.returncode == 0:
        out.update(line for line in untracked.stdout.splitlines() if line)

    return sorted(out)


def is_git_repo(path: Path) -> bool:
    """``True`` iff ``path`` is inside a git working tree (or is one)."""
    if not Path(path).exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def create_git_worktree(
    repo: Path,
    branch_name: str,
    target_dir: Path,
) -> GitWorktreeHandle:
    """Create a new worktree at ``target_dir`` on a fresh branch from
    the repo's current ``HEAD``.

    Raises ``ValueError`` if ``repo`` isn't a git repository,
    ``FileExistsError`` if ``target_dir`` already exists.
    """
    repo = Path(repo)
    target_dir = Path(target_dir)

    if not is_git_repo(repo):
        raise ValueError(f"{repo} is not a git repository")
    if target_dir.exists():
        raise FileExistsError(f"{target_dir} already exists")

    base_sha: str | None = None
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode == 0:
        base_sha = head.stdout.strip() or None

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(target_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return GitWorktreeHandle(repo=repo, path=target_dir, branch=branch_name, base_sha=base_sha)


def cleanup_git_worktree(
    handle: GitWorktreeHandle,
    *,
    delete_branch: bool = False,
) -> None:
    """Remove the worktree and optionally the branch.

    Idempotent — already-removed worktrees and missing branches are
    silently accepted so cleanup-after-error paths don't double-fault.
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(handle.repo),
            "worktree",
            "remove",
            "--force",
            str(handle.path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if delete_branch:
        subprocess.run(
            ["git", "-C", str(handle.repo), "branch", "-D", handle.branch],
            check=False,
            capture_output=True,
            text=True,
        )
