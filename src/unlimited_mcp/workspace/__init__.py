"""Workspace isolation: git worktrees, temp copies, in-place, none.

Public surface:

* :class:`WorkspaceManager` — pick the backend by spec, create + clean up.
* :class:`Workspace` — handle returned by ``manager.create()``; carries
  the path the worker should operate in, plus a ``cleanup()`` callable.
* :func:`resolve_workspace` — turn a preset name (or explicit
  :class:`WorkspaceSpec`) into a fully-resolved spec.
* Backend functions in submodules (``create_git_worktree`` etc.) are
  exposed for tests and direct use.
"""

from .git_worktree import (
    GitWorktreeHandle,
    cleanup_git_worktree,
    create_git_worktree,
    is_git_repo,
)
from .manager import Workspace, WorkspaceManager
from .modes import resolve_workspace
from .temp_copy import TempCopyHandle, cleanup_temp_copy, create_temp_copy

__all__ = [
    "GitWorktreeHandle",
    "TempCopyHandle",
    "Workspace",
    "WorkspaceManager",
    "cleanup_git_worktree",
    "cleanup_temp_copy",
    "create_git_worktree",
    "create_temp_copy",
    "is_git_repo",
    "resolve_workspace",
]
