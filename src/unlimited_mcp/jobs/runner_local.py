# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Background job runner: subprocess.Popen + state files.

Lifecycle
---------
1. :meth:`LocalRunner.submit` spawns a ``subprocess.Popen`` with stdout and
   stderr redirected to disk, records the PID in ``state.json``, and returns
   ``JobResult(status="running")`` immediately.
2. A daemon watcher thread waits for the process to exit, applies secret
   redaction to the log files, and atomically writes the final
   ``result.json`` via :class:`~unlimited_mcp.jobs.store.JobStore`.
3. :meth:`LocalRunner.get_result` reads ``result.json``; if the status is
   still ``"running"`` but the recorded PID is gone, it promotes the job to
   ``"failed"`` (zombie detection — handles MCP server restarts).

Survival across MCP server restarts
-------------------------------------
``start_new_session=True`` detaches the child from the parent's process group
so it keeps running after the MCP process exits.  The watcher thread is a
daemon and dies with the parent; on the next server start, the first
``get_result`` call detects the zombie and marks it failed.  Phase 2 adds
``ts`` for hard durability.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO

from unlimited_mcp.jobs.result import CommandRecord, ErrorBlock, JobResult, JobStatus
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.safety.redactor import Redactor


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(json.dumps(state, default=str), encoding="utf-8")


def _read_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but belongs to a different user


class LocalRunner:
    """Background job runner backed by ``subprocess.Popen`` and state files.

    Parameters
    ----------
    store:
        :class:`~unlimited_mcp.jobs.store.JobStore` that owns the on-disk
        layout for this runner.
    redactor:
        When provided, applied to ``stdout.log`` and ``stderr.log`` after the
        process exits, before ``result.json`` is written.
    """

    def __init__(
        self,
        store: JobStore,
        redactor: Redactor | None = None,
    ) -> None:
        self._store = store
        self._redactor = redactor
        self._idempotency: dict[str, str] = {}
        self._watchers: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        argv: list[str],
        *,
        label: str = "",
        tag: str | None = None,
        timeout_seconds: int = 600,
        idempotency_key: str | None = None,
        env_extra: dict[str, str] | None = None,
        cwd: str | None = None,
        tool: str = "run_command",
        branch: str | None = None,
        worktree_path: str | None = None,
        cleanup_fn: Callable[[], None] | None = None,
        stdin_content: str | None = None,
        prompt_file_content: str | None = None,
        job_id: str | None = None,
    ) -> JobResult:
        """Spawn *argv* in the background and return immediately.

        Returns
        -------
        JobResult
            ``status="running"`` with the new ``job_id``, or the existing
            result when *idempotency_key* matches a non-failed job.
        """
        if idempotency_key:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                existing = self._store.read_result(existing_id)
                if existing and existing.status not in ("failed", "cancelled"):
                    return existing

        job_id = job_id or JobStore.make_job_id(tool)
        started_at = datetime.now(UTC)
        self._store.create(job_id)
        self._store.write_meta(
            job_id,
            {
                "argv": argv,
                "label": label,
                "tag": tag,
                "tool": tool,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "idempotency_key": idempotency_key,
            },
        )

        job_dir = self._store.job_dir(job_id)

        # Resolve prompt_file: write content and substitute {prompt_file} in argv.
        if prompt_file_content is not None:
            pf = job_dir / "prompt.txt"
            pf.write_text(prompt_file_content, encoding="utf-8")
            argv = [str(pf) if tok == "{prompt_file}" else tok for tok in argv]

        # Resolve stdin: write to job_dir/stdin.txt and open as pipe.
        stdin_fh: BinaryIO | int
        if stdin_content is not None:
            sf = job_dir / "stdin.txt"
            sf.write_text(stdin_content, encoding="utf-8")
            stdin_fh = sf.open("rb")
        else:
            stdin_fh = subprocess.DEVNULL

        stdout_path = self._store.stdout_path(job_id)
        stderr_path = self._store.stderr_path(job_id)
        stdout_fh: BinaryIO = stdout_path.open("wb")
        stderr_fh: BinaryIO = stderr_path.open("wb")

        env = {**os.environ, **(env_extra or {})}
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            argv,
            stdin=stdin_fh,
            stdout=stdout_fh,
            stderr=stderr_fh,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        # Close our handle to stdin.txt after Popen (child has its own fd).
        if stdin_content is not None and hasattr(stdin_fh, "close"):
            stdin_fh.close()  # type: ignore[union-attr]

        _write_state(
            self._state_path(job_id),
            {
                "status": "running",
                "pid": proc.pid,
                "started_at": started_at.isoformat(),
                "exit_code": None,
                "finished_at": None,
            },
        )
        initial = JobResult(
            ok=False,
            job_id=job_id,
            status="running",
            tool=tool,
            tag=tag,
            started_at=started_at,
            summary=label or None,
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(initial)

        t = threading.Thread(
            target=self._watch,
            args=(job_id, proc, stdout_fh, stderr_fh, started_at, timeout_seconds, tool,
                  branch, worktree_path, cleanup_fn),
            daemon=True,
            name=f"watcher-{job_id}",
        )
        t.start()
        self._watchers[job_id] = t

        if idempotency_key:
            self._idempotency[idempotency_key] = job_id

        return initial

    def get_result(self, job_id: str) -> JobResult | None:
        """Return the current :class:`~unlimited_mcp.jobs.result.JobResult`,
        or ``None`` if the job is unknown.  Performs zombie detection."""
        result = self._store.read_result(job_id)
        if result is None:
            return None
        if result.status == "running":
            result = self._check_zombie(job_id, result)
        return result

    def cancel(self, job_id: str) -> JobResult:
        """Send ``SIGTERM`` to the job's process and mark it cancelled."""
        now = datetime.now(UTC)
        result = self._store.read_result(job_id)
        if result is None:
            return JobResult(
                ok=False,
                job_id=job_id,
                status="failed",
                tool="unknown",
                started_at=now,
                finished_at=now,
                summary=f"Job {job_id!r} not found.",
                error=ErrorBlock(
                    code="JOB_NOT_FOUND",
                    message=f"No job with id {job_id!r}.",
                    hint="Call list_jobs() to see available job IDs.",
                ),
            )
        if result.status not in ("running", "queued"):
            return result

        state = _read_state(self._state_path(job_id))
        self._cancelled.add(job_id)

        if state:
            pid = state.get("pid")
            if isinstance(pid, int):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)

        cancelled = JobResult(
            ok=False,
            job_id=job_id,
            status="cancelled",
            tool=result.tool,
            started_at=result.started_at,
            finished_at=now,
            summary="Cancelled by orchestrator.",
        )
        self._store.write_result(cancelled)
        return cancelled

    def list_results(self) -> list[JobResult]:
        """Return the current :class:`~unlimited_mcp.jobs.result.JobResult`
        for every known job, with zombie detection applied to running ones."""
        results: list[JobResult] = []
        for job_id in self._store.list_jobs():
            r = self.get_result(job_id)
            if r is not None:
                results.append(r)
        return results

    def join_all(self, timeout: float = 10.0) -> None:
        """Block until all watcher threads finish.  Used in tests."""
        for t in list(self._watchers.values()):
            t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _state_path(self, job_id: str) -> Path:
        return self._store.job_dir(job_id) / "state.json"

    def _check_zombie(self, job_id: str, result: JobResult) -> JobResult:
        # If our own watcher thread is still alive it will write the final
        # result shortly — don't falsely promote to zombie in that race window.
        watcher = self._watchers.get(job_id)
        if watcher is not None and watcher.is_alive():
            return result
        state = _read_state(self._state_path(job_id))
        pid = state.get("pid") if state else None
        if isinstance(pid, int) and not _pid_alive(pid):
            now = datetime.now(UTC)
            failed = JobResult(
                ok=False,
                job_id=job_id,
                status="failed",
                tool=result.tool,
                started_at=result.started_at,
                finished_at=now,
                summary="Worker process died unexpectedly (detected on status check).",
                error=ErrorBlock(
                    code="JOB_ZOMBIE",
                    message="Process exited without writing a result.",
                    hint="Check stderr.log for details.",
                ),
            )
            self._store.write_result(failed)
            return failed
        return result

    def _watch(
        self,
        job_id: str,
        proc: subprocess.Popen[bytes],
        stdout_fh: BinaryIO,
        stderr_fh: BinaryIO,
        started_at: datetime,
        timeout_seconds: int,
        tool: str,
        branch: str | None = None,
        worktree_path: str | None = None,
        cleanup_fn: Callable[[], None] | None = None,
    ) -> None:
        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            timed_out = True
        finally:
            stdout_fh.close()
            stderr_fh.close()

        exit_code = proc.returncode

        if self._redactor is not None:
            for path in (
                self._store.stdout_path(job_id),
                self._store.stderr_path(job_id),
            ):
                if path.exists():
                    path.write_bytes(self._redactor.redact_bytes(path.read_bytes()))

        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        ok = exit_code == 0 and not timed_out
        status: JobStatus = "completed" if ok else "failed"

        summary: str
        if timed_out:
            summary = f"Killed after {timeout_seconds}s timeout."
        elif not ok:
            stderr_tail = b""
            sp = self._store.stderr_path(job_id)
            if sp.exists():
                stderr_tail = sp.read_bytes()[-500:]
            last_line = stderr_tail.decode("utf-8", errors="replace").strip().splitlines()
            summary = last_line[-1][:500] if last_line else f"exit_code={exit_code}"
        else:
            summary = "Completed successfully."

        # Don't overwrite a cancel() — check both in-memory flag and persisted status.
        if job_id in self._cancelled:
            return
        existing = self._store.read_result(job_id)
        if existing is not None and existing.status == "cancelled":
            return

        result = JobResult(
            ok=ok,
            job_id=job_id,
            status=status,
            tool=tool,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            summary=summary,
            raw_output_ref=str(self._store.stdout_path(job_id)),
            commands_run=[CommandRecord(argv=[], exit_code=exit_code, duration_ms=duration_ms)],
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(result)
        _write_state(
            self._state_path(job_id),
            {
                "status": status,
                "pid": proc.pid,
                "started_at": started_at.isoformat(),
                "exit_code": exit_code,
                "finished_at": finished_at.isoformat(),
            },
        )
        if cleanup_fn is not None:
            with contextlib.suppress(Exception):
                cleanup_fn()
