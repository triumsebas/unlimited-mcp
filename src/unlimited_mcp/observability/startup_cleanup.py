"""Non-fatal cleanup tasks that run once at server startup.

Three independent sweeps, each logged and swallowed on error:

1. ``cleanup_orphaned_worktrees`` — physical dirs in ``state/work/`` whose
   source repo has been deleted.  Safe because the source repo is gone so
   the git registration is already broken.

2. ``trim_jsonl`` — remove lines older than *max_age_days* from a JSONL log
   file (errors.jsonl, exec.jsonl).  Atomic: writes to a .tmp file then
   replaces in one os.replace() call.

3. ``cleanup_tmp`` — delete files and directories in an allowed-root tmp
   directory that are older than *max_age_days*.  Never touches items
   referenced by a running job (caller is responsible for passing active
   paths if needed — for /tmp/unlimited-mcp we take the simple approach
   and only delete items older than the threshold).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path


def cleanup_orphaned_worktrees(work_dir: Path) -> list[str]:
    """Remove worktree dirs in *work_dir* whose source repo no longer exists.

    Each git worktree directory contains a ``.git`` text file with the line
    ``gitdir: <repo>/.git/worktrees/<name>``.  We parse that to find the
    source repo.  If ``<repo>/.git`` is gone the worktree is unrecoverable —
    we rmtree the directory.

    Directories without a ``.git`` file (temp copies, unknown) are left alone.
    """
    if not work_dir.exists():
        return []

    removed: list[str] = []
    for entry in work_dir.iterdir():
        if not entry.is_dir():
            continue
        git_file = entry / ".git"
        if not git_file.is_file():
            continue  # temp copy or unknown — don't touch

        content = git_file.read_text(encoding="utf-8", errors="replace").strip()
        if not content.startswith("gitdir:"):
            continue
        # gitdir: /path/to/repo/.git/worktrees/<name>
        gitdir = Path(content[len("gitdir:"):].strip())
        repo_git = gitdir.parent.parent  # /path/to/repo/.git
        if repo_git.exists():
            continue  # source repo still alive

        try:
            shutil.rmtree(entry)
            removed.append(str(entry))
        except OSError:
            pass

    return removed


def trim_jsonl(path: Path, max_age_days: int) -> int:
    """Remove lines older than *max_age_days* from a JSONL file.

    Reads ``timestamp`` or ``ts`` fields (ISO-8601).  Lines that cannot be
    parsed are kept (conservative).  Returns the number of lines removed.
    Writes atomically via a ``.tmp`` sibling.
    """
    if not path.exists():
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    kept: list[str] = []
    removed = 0
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts_raw = entry.get("timestamp") or entry.get("ts") or ""
            if ts_raw:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts < cutoff:
                    removed += 1
                    continue
        except (json.JSONDecodeError, ValueError):
            pass  # keep unparseable lines
        kept.append(line)

    if removed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(path)

    return removed


def cleanup_tmp(tmp_dir: Path, max_age_days: int) -> list[str]:
    """Delete children of *tmp_dir* older than *max_age_days*.

    Only deletes direct children (files and directories), not the root itself.
    Errors on individual items are silently skipped.
    """
    if not tmp_dir.exists():
        return []

    cutoff_ts = datetime.now(UTC).timestamp() - max_age_days * 86400
    removed: list[str] = []

    for child in tmp_dir.iterdir():
        try:
            if child.stat().st_mtime >= cutoff_ts:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed.append(str(child))
        except OSError:
            pass

    return removed
