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
    return GitWorktreeHandle(repo=repo, path=target_dir, branch=branch_name)


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
