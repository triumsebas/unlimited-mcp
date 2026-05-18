# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Temp-copy workspace backend.

For tasks that don't need git history (e.g. "run a tool against a
snapshot of this directory"), we copy the source tree into the runtime
state directory and let the worker scribble freely. Cleanup is a single
``rmtree``.

We exclude the obvious uninteresting subtrees (``.git``,
``__pycache__``, ``.venv``) by default; callers can override via
``ignore_patterns``.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IGNORE: tuple[str, ...] = (".git", "__pycache__", ".venv", "node_modules")


@dataclass(frozen=True)
class TempCopyHandle:
    source: Path
    path: Path


def create_temp_copy(
    source: Path,
    target_dir: Path,
    *,
    ignore_patterns: Sequence[str] = DEFAULT_IGNORE,
) -> TempCopyHandle:
    """Recursively copy ``source`` into ``target_dir`` (which must not
    already exist) and return a handle."""
    source = Path(source)
    target_dir = Path(target_dir)

    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist")
    if target_dir.exists():
        raise FileExistsError(f"{target_dir} already exists")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target_dir,
        ignore=shutil.ignore_patterns(*ignore_patterns),
    )
    return TempCopyHandle(source=source, path=target_dir)


def cleanup_temp_copy(handle: TempCopyHandle) -> None:
    """Remove the temp copy. Idempotent."""
    if handle.path.exists():
        shutil.rmtree(handle.path)
