# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Task-spooler backend for background jobs.

Unlike :class:`~unlimited_mcp.jobs.runner_local.LocalRunner`, ``TsRunner``
delegates scheduling to the ``ts`` (task-spooler) daemon. Jobs survive MCP
server restarts: ``ts`` keeps the queue alive as long as the user's session
runs; each job is executed by a dedicated :mod:`~unlimited_mcp.jobs._ts_worker`
subprocess, so MCP death does not interrupt a running worker.

Requirements: ``task-spooler`` (``brew install task-spooler`` on macOS,
``apt install task-spooler`` on Debian/Ubuntu). If the binary is not found,
:class:`TsNotFoundError` is raised at construction time with an install hint.

A dedicated ``TS_SOCKET`` per MCP server instance isolates our queue from any
other ``ts`` usage on the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from unlimited_mcp.jobs.result import ErrorBlock, JobResult, JobStatus
from unlimited_mcp.jobs.store import JobStore, file_lock


_TERMINAL: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})


def _find_ts_bin() -> str:
    """Return the task-spooler binary available on this system.

    On Linux, the package may install as ``tsp`` to avoid conflicting with the
    ``ts`` binary from ``moreutils``. On macOS (Homebrew) it is always ``ts``.
    We probe ``tsp`` first so Linux systems with both packages use the right one.
    """
    for candidate in ("tsp", "ts"):
        if shutil.which(candidate) is not None:
            return candidate
    return "ts"  # not found — probe in __init__ will raise TsNotFoundError


class TsNotFoundError(RuntimeError):
    """Raised when the ``ts`` binary cannot be located."""


class TsRunner:
    """Queue jobs through ``ts`` (task-spooler) for durable background execution.

    Parameters
    ----------
    store:
        :class:`~unlimited_mcp.jobs.store.JobStore` that owns the on-disk layout.
    ts_socket:
        Path to the ``TS_SOCKET`` file.  Defaults to
        ``state_dir / "ts.sock"``.
    ts_bin:
        Path to the ``ts`` binary. Defaults to ``"ts"`` (resolved from
        ``PATH``); pass the full path if ``ts`` is not on ``PATH``.
    max_slots:
        Maximum number of parallel jobs (``ts -S <n>``). ``None`` keeps the
        ts default (1 — sequential).
    """

    def __init__(
        self,
        store: JobStore,
        *,
        ts_socket: Path | str | None = None,
        ts_bin: str = _find_ts_bin(),
        max_slots: int | None = None,
    ) -> None:
        self._store = store
        self._ts_bin = ts_bin
        self._ts_env = {**os.environ}
        if ts_socket is not None:
            self._ts_env["TS_SOCKET"] = str(ts_socket)

        # Probe that ts exists
        try:
            subprocess.run([ts_bin, "-V"], capture_output=True, check=False)
        except FileNotFoundError:
            raise TsNotFoundError(
                f"ts binary not found at {ts_bin!r}. "
                "Install with: brew install task-spooler  (macOS) "
                "or: apt install task-spooler  (Debian/Ubuntu)"
            )

        if max_slots is not None:
            subprocess.run([ts_bin, "-S", str(max_slots)], env=self._ts_env, check=False)

    # ------------------------------------------------------------------
    # Public API (mirrors LocalRunner)
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
        """Enqueue *argv* in task-spooler and return immediately.

        ``cleanup_fn`` is called by the worker after the job finishes
        (encoded in the worker args), so worktree cleanup is durable across
        MCP restarts.  Callables are not serialisable; ``worktree_path`` is
        used to trigger a ``git worktree remove`` in the worker instead.

        Prompt delivery: files are written to ``job_dir`` before ts submission
        so the worker process finds them on disk without receiving large
        strings on its own command line.
        """
        job_id = job_id or JobStore.make_job_id(tool)
        started_at = datetime.now(UTC)
        self._store.create(job_id)
        job_dir = self._store.job_dir(job_id)

        # Write prompt_file before ts submission; substitute path in argv.
        stdin_file: str | None = None
        if prompt_file_content is not None:
            pf = job_dir / "prompt.txt"
            pf.write_text(prompt_file_content, encoding="utf-8")
            argv = [str(pf) if tok == "{prompt_file}" else tok for tok in argv]

        # Write stdin content to job_dir; worker opens it as stdin.
        if stdin_content is not None:
            sf = job_dir / "stdin.txt"
            sf.write_text(stdin_content, encoding="utf-8")
            stdin_file = str(sf)

        worker_args = json.dumps({
            "argv": argv,
            "cwd": cwd,
            "timeout": timeout_seconds,
            "env_extra": env_extra or {},
            "tool": tool,
            "tag": tag,
            "branch": branch,
            "worktree_path": worktree_path,
            "stdin_file": stdin_file,
        })
        worker_cmd = [sys.executable, "-m", "unlimited_mcp.jobs._ts_worker", str(job_dir), worker_args]
        ts_cmd = [self._ts_bin, "-L", job_id, *worker_cmd]

        # B8: write the initial "running" result BEFORE enqueuing. ts spawns the
        # worker as an independent process that may finish (writing a terminal
        # result.json) before this method continues; writing "running" afterwards
        # would clobber that good result and leave the job "running" forever.
        initial = JobResult(
            ok=False, job_id=job_id, status="running", tool=tool, tag=tag,
            started_at=started_at, summary=label or None,
            branch=branch, worktree_path=worktree_path,
        )
        self._store.write_result(initial)

        ts_result = subprocess.run(ts_cmd, capture_output=True, text=True, env=self._ts_env)
        if ts_result.returncode != 0:
            now = datetime.now(UTC)
            err_msg = ts_result.stderr.strip() or "ts submit failed"
            result = JobResult(
                ok=False, job_id=job_id, status="failed", tool=tool, tag=tag,
                started_at=now, finished_at=now,
                summary=err_msg,
                error=ErrorBlock(code="TS_SUBMIT_FAILED", message=err_msg, retryable=True),
            )
            self._store.write_result(result)
            return result

        slot_id = ts_result.stdout.strip()
        self._store.write_meta(job_id, {
            "argv": argv, "label": label, "tag": tag, "tool": tool,
            "cwd": cwd, "timeout_seconds": timeout_seconds,
            "idempotency_key": idempotency_key, "ts_slot_id": slot_id,
        })

        return initial

    def get_result(self, job_id: str) -> JobResult | None:
        """Return the current result, refreshing from disk if the worker has finished."""
        result = self._store.read_result(job_id)
        if result is None:
            return None
        if result.status not in _TERMINAL:
            # Worker writes result.json when done — just re-read.
            fresh = self._store.read_result(job_id)
            if fresh is not None and fresh.status in _TERMINAL:
                return fresh
        return result

    def cancel(self, job_id: str) -> JobResult:
        """Kill the ts slot for *job_id* and mark it cancelled."""
        now = datetime.now(UTC)
        result = self._store.read_result(job_id)
        if result is None:
            return JobResult(
                ok=False, job_id=job_id, status="failed", tool="unknown",
                started_at=now, finished_at=now,
                summary=f"Job {job_id!r} not found.",
                error=ErrorBlock(code="JOB_NOT_FOUND",
                                 message=f"No job with id {job_id!r}.",
                                 hint="Call list_jobs() to see available job IDs."),
            )
        if result.status in _TERMINAL:
            return result

        # B4 (cross-process): the worker runs in a separate process, so a
        # threading.Lock cannot serialize against its final write — take the
        # per-job file lock. Policy: cancel wins. If the job was still running
        # when cancel was called, we write "cancelled" even if the worker
        # finished concurrently; the worker re-checks under the same lock and
        # refuses to overwrite a "cancelled" result. Both orderings converge on
        # "cancelled" deterministically.
        with file_lock(self._store.lock_path(job_id)):
            meta = self._store.read_meta(job_id)
            slot_id = meta.get("ts_slot_id") if meta else None
            if slot_id:
                subprocess.run([self._ts_bin, "-k", str(slot_id)],
                               env=self._ts_env, capture_output=True, check=False)

            cancelled = result.model_copy(update={
                "ok": False, "status": "cancelled",
                "finished_at": now, "summary": "Cancelled by orchestrator.",
            })
            self._store.write_result(cancelled)
            return cancelled

    def list_results(self) -> list[JobResult]:
        """Return all known results with stale-running detection."""
        results: list[JobResult] = []
        for job_id in self._store.list_jobs():
            r = self.get_result(job_id)
            if r is not None:
                results.append(r)
        return results

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def ts_queue(self) -> list[dict[str, Any]]:
        """Return the raw ``ts -l`` output as a list of dicts, for diagnostics."""
        proc = subprocess.run([self._ts_bin, "-l"], capture_output=True, text=True, env=self._ts_env)
        rows: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines()[1:]:  # skip header
            parts = line.split(None, 7)
            if len(parts) >= 3:
                rows.append({"slot_id": parts[0], "state": parts[1], "command": " ".join(parts[3:])})
        return rows
