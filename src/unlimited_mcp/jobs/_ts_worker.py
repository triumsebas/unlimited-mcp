# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Worker entry point executed by task-spooler for each TsRunner job.

Invoked as::

    python3 -m unlimited_mcp.jobs._ts_worker <job_dir> <json_args>

where ``json_args`` is a JSON object with keys:
  argv, cwd, timeout, env_extra, tool, tag, branch, worktree_path

The worker:
1. Writes ``state.json`` (running).
2. Runs *argv* with stdout/stderr redirected to ``stdout.log`` / ``stderr.log``.
3. Writes ``result.json`` on completion.
4. Removes the git worktree when ``worktree_path`` is set (safe_dev cleanup).

Timeout contract
----------------
``timeout`` is **execution time only** — it starts when this worker process
spawns the subprocess, not when the job was submitted or queued in ts.
If the MCP server (Claude) shuts down while the job is running, ts keeps
this worker alive and the timeout continues to apply normally.  Jobs that
finish (or time out) while the server is down are written to ``result.json``
and will appear in the inbox on the next server startup.

Recommended timeout values (pass as ``timeout_seconds`` to the tool):

  * Docstrings / quick refactor        →  1 800 s  (30 min)
  * New feature / test suite           →  3 600 s  (60 min)
  * Complex multi-file task            → 14 400 s  ( 4 h)
  * Local GPU-backed LLM               → 86 400 s  ( 1 day)

When in doubt, overestimate — a generous timeout has no cost if the job
finishes early; a short timeout kills work in progress.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _write_json(path: Path, data: object) -> None:
    import tempfile

    tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_status(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("status") if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_result_unless_cancelled(job_dir: Path, result: object) -> None:
    """Write ``result.json`` under the per-job file lock, unless a concurrent
    ``TsRunner.cancel()`` (separate process) already wrote ``"cancelled"``.

    Policy: cancel wins. flock coordinates cross-process; a ``threading.Lock``
    would not, since the worker runs in its own process.
    """
    from unlimited_mcp.jobs.store import file_lock

    result_path = job_dir / "result.json"
    with file_lock(job_dir / ".lock"):
        if _read_status(result_path) != "cancelled":
            _write_json(result_path, result)


def _remove_worktree(worktree_path: str) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True,
            check=False,
        )


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: _ts_worker <job_dir> <json_args>")

    job_dir = Path(sys.argv[1])
    args: dict[str, Any] = json.loads(sys.argv[2])

    argv: list[str] = args["argv"]
    cwd: str | None = args.get("cwd")
    timeout: int = int(args.get("timeout", 600))
    env_extra: dict[str, str] = args.get("env_extra") or {}
    tool: str = args.get("tool", "run_command")
    tag: str | None = args.get("tag")
    branch: str | None = args.get("branch")
    worktree_path: str | None = args.get("worktree_path")
    worktree_base: str | None = args.get("worktree_base")
    quality_gate_enabled: bool = bool(args.get("quality_gate_enabled", True))
    stdin_file: str | None = args.get("stdin_file")

    started_at = datetime.now(UTC)
    state_path = job_dir / "state.json"

    _write_json(
        state_path,
        {
            "status": "running",
            "pid": os.getpid(),
            "started_at": started_at.isoformat(),
            "exit_code": None,
            "finished_at": None,
        },
    )

    env = {**os.environ, **env_extra}
    timed_out = False
    exit_code: int = -1

    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"

    # Conditionally a file or DEVNULL; closed in the finally below. A `with`
    # can't express the DEVNULL branch cleanly, so suppress SIM115 here.
    stdin_fh = open(stdin_file, "rb") if stdin_file else subprocess.DEVNULL  # noqa: SIM115
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        try:
            proc = subprocess.run(
                argv,
                stdin=stdin_fh,
                stdout=out,
                stderr=err,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
        except Exception as exc:
            err.write(str(exc).encode())
            exit_code = -1
        finally:
            if stdin_file and hasattr(stdin_fh, "close"):
                stdin_fh.close()

    finished_at = datetime.now(UTC)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    _write_json(
        state_path,
        {
            "status": "finished",
            "pid": os.getpid(),
            "started_at": started_at.isoformat(),
            "exit_code": exit_code,
            "finished_at": finished_at.isoformat(),
        },
    )

    ok = exit_code == 0 and not timed_out
    if timed_out:
        summary = f"Timed out after {timeout}s."
        error = {
            "code": "TIMEOUT",
            "message": f"Process exceeded {timeout}s.",
            "hint": "Increase timeout_seconds.",
            "retryable": True,
        }
    elif exit_code != 0:
        tail = b""
        for p in (stderr_path, stdout_path):
            if p.exists():
                tail = p.read_bytes()[-2048:]
                if tail:
                    break
        summary = (
            tail.decode("utf-8", errors="replace").strip()[-200:]
            or f"Exited with code {exit_code}."
        )
        error = {
            "code": "NONZERO_EXIT",
            "message": f"Process exited with code {exit_code}.",
            "hint": "Check raw_output_ref for details.",
            "retryable": False,
        }
    else:
        _content = b""
        for _p in (stdout_path, stderr_path):
            if _p.exists():
                _content = _p.read_bytes()[-500:]
                if _content.strip():
                    break
        summary = (
            _content.decode("utf-8", errors="replace").strip()[-500:] or "Completed successfully."
        )
        error = None

    # Post-process while the worktree still exists (removed at the end):
    # changed_files, quality gate, conflict detection. Never fatal.
    changed_files: list[str] = []
    quality_gate: dict[str, object] | None = None
    warnings: list[dict[str, object]] = []
    try:
        from unlimited_mcp.jobs.postprocess import run_postprocess
        from unlimited_mcp.jobs.store import JobStore

        post = run_postprocess(
            job_dir.name,
            worktree_path,
            worktree_base,
            JobStore(job_dir.parent),
            quality_gate_enabled=quality_gate_enabled,
        )
        changed_files = post.changed_files
        quality_gate = post.quality_gate.model_dump(mode="json") if post.quality_gate else None
        warnings = [w.model_dump(mode="json") for w in post.warnings]
    except Exception:
        pass

    result = {
        "ok": ok,
        "job_id": job_dir.name,
        "status": "completed" if ok else "failed",
        "tool": tool,
        "tag": tag,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "seen_at": None,
        "duration_ms": duration_ms,
        "summary": summary,
        "changed_files": changed_files,
        "diff_ref": None,
        "branch": branch,
        "worktree_path": worktree_path,
        "commands_run": [
            {
                "argv": argv,
                "cwd": cwd,
                "host": "local",
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "safety_class": "unknown",
                "risk_level": "low",
                "blast_radius": "local",
                "confirm_token_used": False,
            }
        ],
        "tests": None,
        "quality_gate": quality_gate,
        "artifacts": [],
        "warnings": warnings,
        "questions": [],
        "raw_output_ref": str(stdout_path),
        "output_truncated": False,
        "output_bytes": stdout_path.stat().st_size if stdout_path.exists() else 0,
        "risk_level": "low",
        "blast_radius": "local",
        "error": error,
        "confirm_token": None,
        "confirm_reason": None,
    }
    # B4 (cross-process): serialize against TsRunner.cancel() — cancel wins.
    _write_result_unless_cancelled(job_dir, result)

    if worktree_path:
        _remove_worktree(worktree_path)


if __name__ == "__main__":
    main()
