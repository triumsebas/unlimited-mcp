"""RemoteRunner: runs a Host.run() call in a background thread.

Unlike :class:`~unlimited_mcp.jobs.runner_local.LocalRunner` (which spawns
a subprocess and tracks its PID), ``RemoteRunner`` calls ``Host.run()``
synchronously inside a daemon thread.  The same :class:`JobStore` is used
so :func:`get_job_result` works identically for local and remote jobs.

Cancellation
------------
Cancel marks the job as ``"cancelled"`` in the store; the background thread
checks this before writing its final result and exits silently.  There is no
remote SIGTERM — the running command will finish (or timeout) on its own.
For SSH hosts, the timeout passed to ``submit`` is enforced by paramiko's
channel timeout, which closes the connection when exceeded.

Workspace / stdin / prompt_file
--------------------------------
Remote execution skips git worktree management (the worktree would need to
exist on the remote machine).  ``stdin_content`` and ``prompt_file_content``
are not supported for remote runners and will raise ``NotImplementedError``
if provided.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from unlimited_mcp.hosts.base import Host, RunOutput
from unlimited_mcp.jobs.result import CommandRecord, ErrorBlock, JobResult, JobStatus
from unlimited_mcp.jobs.store import JobStore

log = logging.getLogger(__name__)


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(json.dumps(state, default=str), encoding="utf-8")


class RemoteRunner:
    """Background job runner backed by ``Host.run()`` in a daemon thread.

    Parameters
    ----------
    host:
        The execution backend (typically an :class:`~unlimited_mcp.hosts.ssh.SshHost`).
    store:
        Shared :class:`~unlimited_mcp.jobs.store.JobStore` — same instance as
        the ``LocalRunner`` so all jobs are visible through a single store.
    """

    def __init__(self, host: Host, store: JobStore) -> None:
        self._host = host
        self._store = store
        self._cancelled: set[str] = set()
        self._watchers: dict[str, threading.Thread] = {}
        self._idempotency: dict[str, str] = {}

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
        """Submit *argv* to the remote host and return immediately with ``status="running"``."""

        if idempotency_key:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                existing = self._store.read_result(existing_id)
                if existing and existing.status not in ("failed", "cancelled"):
                    return existing

        job_id = job_id or JobStore.make_job_id(tool)
        started_at = datetime.now(UTC)
        self._store.create(job_id)

        # Resolve prompt_file: upload via SFTP and substitute {prompt_file} in argv.
        remote_prompt_file: str | None = None
        if prompt_file_content is not None:
            from unlimited_mcp.hosts.ssh import SshHost as _SshHost
            if not isinstance(self._host, _SshHost):
                raise NotImplementedError(
                    "prompt_file_content requires an SshHost with SFTP support."
                )
            remote_prompt_file = f"/tmp/umcp-{job_id}-prompt.txt"
            self._host.sftp_put(remote_prompt_file, prompt_file_content.encode())
            argv = [remote_prompt_file if tok == "{prompt_file}" else tok for tok in argv]

        # Resolve stdin: encode for SSH channel write.
        stdin_bytes: bytes | None = stdin_content.encode() if stdin_content is not None else None
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
                "exec_host": self._host.name,
            },
        )

        # Write state without a PID so LocalRunner.cancel() skips SIGTERM.
        _write_state(
            self._store.job_dir(job_id) / "state.json",
            {
                "status": "running",
                "pid": None,
                "exec_host": self._host.name,
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
            summary=f"[{self._host.name}] {label}" if label else f"[{self._host.name}] running",
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(initial)

        t = threading.Thread(
            target=self._watch,
            args=(job_id, argv, cwd, env_extra, timeout_seconds, tool,
                  started_at, branch, worktree_path, cleanup_fn,
                  stdin_bytes, remote_prompt_file),
            daemon=True,
            name=f"remote-watcher-{job_id}",
        )
        t.start()
        self._watchers[job_id] = t
        if idempotency_key:
            self._idempotency[idempotency_key] = job_id
        return initial

    def get_result(self, job_id: str) -> JobResult | None:
        return self._store.read_result(job_id)

    def cancel(self, job_id: str) -> JobResult:
        """Mark the job as cancelled; the background thread will not overwrite it."""
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

        self._cancelled.add(job_id)
        cancelled = JobResult(
            ok=False,
            job_id=job_id,
            status="cancelled",
            tool=result.tool,
            started_at=result.started_at,
            finished_at=now,
            summary=f"Cancelled by orchestrator (remote job on {self._host.name}).",
        )
        self._store.write_result(cancelled)
        return cancelled

    def list_results(self) -> list[JobResult]:
        results: list[JobResult] = []
        for job_id in self._store.list_jobs():
            r = self.get_result(job_id)
            if r is not None:
                results.append(r)
        return results

    def join_all(self, timeout: float = 10.0) -> None:
        for t in list(self._watchers.values()):
            t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _watch(
        self,
        job_id: str,
        argv: list[str],
        cwd: str | None,
        env_extra: dict[str, str] | None,
        timeout_seconds: int,
        tool: str,
        started_at: datetime,
        branch: str | None,
        worktree_path: str | None,
        cleanup_fn: Callable[[], None] | None,
        stdin_bytes: bytes | None = None,
        remote_prompt_file: str | None = None,
    ) -> None:
        timed_out = False
        output: RunOutput | None = None

        try:
            output = self._host.run(
                argv,
                cwd=cwd,
                env_extra=env_extra,
                timeout_seconds=timeout_seconds,
                stdout_path=self._store.stdout_path(job_id),
                stderr_path=self._store.stderr_path(job_id),
                stdin_content=stdin_bytes,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning("Remote job %s timed out after %ds on %s", job_id, timeout_seconds, self._host.name)
        except Exception as exc:
            log.error("Remote job %s failed on %s: %s", job_id, self._host.name, exc)
            self._write_failed(job_id, tool, started_at, str(exc), branch, worktree_path)
            if cleanup_fn is not None:
                _safe_call(cleanup_fn)
            return

        # Ensure output files exist — SshHost writes them via stdout_path;
        # fallback for other Host implementations that may not.
        if output is not None:
            stdout_path = self._store.stdout_path(job_id)
            stderr_path = self._store.stderr_path(job_id)
            if not stdout_path.exists():
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_path.write_bytes(output.stdout)
            if not stderr_path.exists():
                stderr_path.parent.mkdir(parents=True, exist_ok=True)
                stderr_path.write_bytes(output.stderr)

        # Don't overwrite a cancel()
        if job_id in self._cancelled:
            return
        existing = self._store.read_result(job_id)
        if existing is not None and existing.status == "cancelled":
            return

        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

        if timed_out or output is None:
            summary = f"Killed after {timeout_seconds}s timeout on {self._host.name}."
            ok = False
            status: JobStatus = "failed"
            exit_code = -1
        else:
            exit_code = output.exit_code
            ok = exit_code == 0
            status = "completed" if ok else "failed"
            if not ok:
                stderr_tail = self._store.stderr_path(job_id)
                last_lines = (
                    stderr_tail.read_bytes()[-500:].decode("utf-8", errors="replace").strip().splitlines()
                    if stderr_tail.exists()
                    else []
                )
                summary = last_lines[-1][:500] if last_lines else f"exit_code={exit_code}"
            else:
                summary = f"Completed on {self._host.name}."

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
            commands_run=[CommandRecord(argv=argv, exit_code=exit_code, duration_ms=duration_ms)],
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(result)

        if remote_prompt_file is not None:
            try:
                self._host.run(["rm", "-f", remote_prompt_file])
            except Exception:
                pass

        if cleanup_fn is not None:
            _safe_call(cleanup_fn)

    def _write_failed(
        self,
        job_id: str,
        tool: str,
        started_at: datetime,
        message: str,
        branch: str | None,
        worktree_path: str | None,
    ) -> None:
        if job_id in self._cancelled:
            return
        now = datetime.now(UTC)
        self._store.write_result(JobResult(
            ok=False,
            job_id=job_id,
            status="failed",
            tool=tool,
            started_at=started_at,
            finished_at=now,
            duration_ms=int((now - started_at).total_seconds() * 1000),
            summary=message[:500],
            error=ErrorBlock(
                code="REMOTE_EXEC_ERROR",
                message=message,
                hint=f"Check SSH connectivity to {self._host.name}.",
            ),
            branch=branch,
            worktree_path=worktree_path,
        ))


def _safe_call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        pass
