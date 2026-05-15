"""Workspace orchestration: spec → backend → :class:`Workspace` handle.

The manager picks the right backend (``current``, ``git_worktree``,
``temp_copy``, ``none``) given a resolved :class:`WorkspaceSpec`, and
returns a :class:`Workspace` handle the caller passes to the worker
runner. The handle exposes ``cleanup()`` so the runner doesn't need to
care which backend was used.

``remote_cwd`` is reserved for phase 3 and currently raises
``NotImplementedError``.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from unlimited_mcp.config.schema import Knowledge, WorkspaceSpec

from .git_worktree import cleanup_git_worktree, create_git_worktree
from .modes import resolve_workspace
from .temp_copy import cleanup_temp_copy, create_temp_copy


def _noop() -> None:
    """Cleanup callable for backends that don't own anything to remove."""


@dataclass
class Workspace:
    """Active workspace handle returned by :meth:`WorkspaceManager.create`.

    ``path`` is where the worker should operate. ``branch`` is set only
    for ``git_worktree`` mode. ``cleanup()`` removes any backend-owned
    state (worktree, temp dir); for ``current``/``none`` it's a no-op so
    callers can always call it unconditionally.
    """

    spec: WorkspaceSpec
    path: Path
    branch: str | None = None
    _cleanup_fn: Callable[[], None] = field(default=_noop, repr=False)

    def cleanup(self) -> None:
        self._cleanup_fn()


def _make_label(prefix: str) -> str:
    """Sortable, unique target subdirectory name."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{ts}-{secrets.token_hex(3)}"


class WorkspaceManager:
    """Stateless factory: holds the knowledge catalog and the runtime
    base directory under which transient workspaces (worktrees, copies)
    are created."""

    def __init__(self, knowledge: Knowledge, base_dir: Path) -> None:
        self.knowledge = knowledge
        self.base_dir = Path(base_dir)

    def create(
        self,
        spec: str | WorkspaceSpec | None,
        *,
        source: Path | None = None,
        label: str = "delegate",
    ) -> Workspace:
        """Create a workspace per ``spec`` and return a handle.

        ``source`` is required for ``git_worktree`` and ``temp_copy``
        modes; for ``current`` it defaults to the current working
        directory; for ``none`` it is ignored.
        """
        resolved = resolve_workspace(spec, self.knowledge)

        if resolved.mode == "current":
            path = Path(source) if source is not None else Path.cwd()
            return Workspace(spec=resolved, path=path, branch=None, _cleanup_fn=_noop)

        if resolved.mode == "none":
            return Workspace(
                spec=resolved,
                path=Path.cwd(),
                branch=None,
                _cleanup_fn=_noop,
            )

        if resolved.mode == "git_worktree":
            if source is None:
                raise ValueError("git_worktree mode requires `source` (path to a git repo)")
            unique_label = _make_label(label)
            target = self.base_dir / unique_label
            branch_name = f"unlimited-mcp/{unique_label}"
            git_handle = create_git_worktree(Path(source), branch_name, target)
            return Workspace(
                spec=resolved,
                path=git_handle.path,
                branch=git_handle.branch,
                _cleanup_fn=lambda: cleanup_git_worktree(git_handle),
            )

        if resolved.mode == "temp_copy":
            if source is None:
                raise ValueError("temp_copy mode requires `source` directory")
            target = self.base_dir / _make_label(label)
            copy_handle = create_temp_copy(Path(source), target)
            return Workspace(
                spec=resolved,
                path=copy_handle.path,
                branch=None,
                _cleanup_fn=lambda: cleanup_temp_copy(copy_handle),
            )

        if resolved.mode == "remote_cwd":
            raise NotImplementedError(
                "remote_cwd workspaces are phase 3 (need an SSH host backend)"
            )

        raise ValueError(f"Unsupported workspace mode: {resolved.mode!r}")
