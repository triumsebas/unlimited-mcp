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

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def _write_json(path: Path, data: object) -> None:
    import tempfile

    tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_worktree(worktree_path: str) -> None:
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True,
            check=False,
        )
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: _ts_worker <job_dir> <json_args>")

    job_dir = Path(sys.argv[1])
    args: dict = json.loads(sys.argv[2])

    argv: list[str] = args["argv"]
    cwd: str | None = args.get("cwd")
    timeout: int = int(args.get("timeout", 600))
    env_extra: dict[str, str] = args.get("env_extra") or {}
    tool: str = args.get("tool", "run_command")
    tag: str | None = args.get("tag")
    branch: str | None = args.get("branch")
    worktree_path: str | None = args.get("worktree_path")
    stdin_file: str | None = args.get("stdin_file")

    started_at = datetime.now(UTC)
    state_path = job_dir / "state.json"

    _write_json(state_path, {
        "status": "running",
        "pid": os.getpid(),
        "started_at": started_at.isoformat(),
        "exit_code": None,
        "finished_at": None,
    })

    env = {**os.environ, **env_extra}
    timed_out = False
    exit_code: int = -1

    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"

    stdin_fh = open(stdin_file, "rb") if stdin_file else subprocess.DEVNULL  # noqa: WPS515
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
                stdin_fh.close()  # type: ignore[union-attr]

    finished_at = datetime.now(UTC)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    _write_json(state_path, {
        "status": "finished",
        "pid": os.getpid(),
        "started_at": started_at.isoformat(),
        "exit_code": exit_code,
        "finished_at": finished_at.isoformat(),
    })

    ok = exit_code == 0 and not timed_out
    if timed_out:
        summary = f"Timed out after {timeout}s."
        error = {"code": "TIMEOUT", "message": f"Process exceeded {timeout}s.", "hint": "Increase timeout_seconds.", "retryable": True}
    elif exit_code != 0:
        tail = b""
        for p in (stderr_path, stdout_path):
            if p.exists():
                tail = p.read_bytes()[-2048:]
                if tail:
                    break
        summary = tail.decode("utf-8", errors="replace").strip()[-200:] or f"Exited with code {exit_code}."
        error = {"code": "NONZERO_EXIT", "message": f"Process exited with code {exit_code}.", "hint": "Check raw_output_ref for details.", "retryable": False}
    else:
        summary = "Completed successfully."
        error = None

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
        "changed_files": [],
        "diff_ref": None,
        "branch": branch,
        "worktree_path": worktree_path,
        "commands_run": [{"argv": argv, "cwd": cwd, "host": "local", "exit_code": exit_code,
                          "duration_ms": duration_ms, "safety_class": "unknown",
                          "risk_level": "low", "blast_radius": "local", "confirm_token_used": False}],
        "tests": None,
        "artifacts": [],
        "warnings": [],
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
    _write_json(job_dir / "result.json", result)

    if worktree_path:
        _remove_worktree(worktree_path)


if __name__ == "__main__":
    main()
