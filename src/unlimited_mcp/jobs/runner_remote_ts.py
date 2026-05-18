# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""RemoteTsRunner: submits jobs to task-spooler on a remote SSH host.

Unlike :class:`~unlimited_mcp.jobs.runner_remote.RemoteRunner` (which blocks
an SSH channel until the command finishes), ``RemoteTsRunner`` enqueues the
command on the *remote* ``ts``/``tsp`` daemon via :meth:`SshHost.ts_submit`
and returns immediately.  A lightweight daemon thread polls the remote ts
status every *poll_interval* seconds and writes the final :class:`JobResult`
to the :class:`JobStore` when the job completes.

Because the job lives in the remote daemon, the SSH connection can be
interrupted and re-established without losing the job.

Requirements
------------
- ``paramiko`` installed locally (``pip install 'unlimited-mcp[ssh]'``)
- ``task-spooler`` (``ts`` or ``tsp``) installed on the **remote** host::

      # Debian/Ubuntu
      apt install task-spooler

      # macOS (local or remote via Homebrew-over-ssh)
      brew install task-spooler

The binary is auto-detected on the remote via :meth:`SshHost._find_remote_ts_bin`.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import json

from unlimited_mcp.hosts.ssh import SshHost
from unlimited_mcp.jobs.result import CommandRecord, ErrorBlock, JobResult, JobStatus
from unlimited_mcp.jobs.store import JobStore

log = logging.getLogger(__name__)

_TERMINAL: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})


class RemoteTsRunner:
    """Background job runner backed by task-spooler on a remote SSH host.

    Parameters
    ----------
    host:
        An :class:`~unlimited_mcp.hosts.ssh.SshHost` instance pointing at the
        remote machine where task-spooler is installed.
    store:
        Shared :class:`~unlimited_mcp.jobs.store.JobStore` — same instance as
        other runners so all jobs are visible through a single store.
    ts_socket:
        Optional path to the ``TS_SOCKET`` file on the **remote** machine.
        When ``None``, the remote ``ts``/``tsp`` default socket is used.
    poll_interval:
        Seconds between status polls. Default 5 s is a reasonable trade-off
        between responsiveness and SSH round-trip cost.
    """

    def __init__(
        self,
        host: SshHost,
        store: JobStore,
        *,
        ts_socket: str | None = None,
        max_slots: int | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self._host = host
        self._store = store
        self._ts_socket = ts_socket
        self._max_slots = max_slots
        self._poll_interval = poll_interval
        self._cancelled: set[str] = set()
        self._slot_map: dict[str, int] = {}  # job_id → remote ts slot_id
        self._watchers: dict[str, threading.Thread] = {}
        self._slots_configured = False  # lazy: applied on first submit

    # ------------------------------------------------------------------
    # Public API (mirrors RemoteRunner / LocalRunner)
    # ------------------------------------------------------------------

    def submit(
        self,
        argv: list[str],
        *,
        label: str = "",
        tag: str | None = None,
        timeout_seconds: int = 3600,
        env_extra: dict[str, str] | None = None,
        cwd: str | None = None,
        tool: str = "run_command",
        branch: str | None = None,
        worktree_path: str | None = None,
        cleanup_fn: Callable[[], None] | None = None,
        stdin_content: str | None = None,
        prompt_file_content: str | None = None,
        job_id: str | None = None,
        remote_questions_dir: str | None = None,
    ) -> JobResult:
        """Enqueue *argv* on the remote task-spooler and return immediately."""
        self._ensure_slots_configured()

        job_id = job_id or JobStore.make_job_id(tool)
        started_at = datetime.now(UTC)

        # Resolve prompt_file: upload via SFTP and substitute {prompt_file} in argv.
        remote_prompt_file: str | None = None
        if prompt_file_content is not None:
            remote_prompt_file = f"/tmp/umcp-{job_id}-prompt.txt"
            self._host.sftp_put(remote_prompt_file, prompt_file_content.encode())
            argv = [remote_prompt_file if tok == "{prompt_file}" else tok for tok in argv]

        # Resolve stdin: upload to a temp file; ts_submit will pipe it to the command.
        remote_stdin_file: str | None = None
        if stdin_content is not None:
            remote_stdin_file = f"/tmp/umcp-{job_id}-stdin.txt"
            self._host.sftp_put(remote_stdin_file, stdin_content.encode())

        # Temp file on remote to capture the exit code.
        ec_path = f"/tmp/.umcp-{job_id}.ec"

        try:
            slot_id = self._host.ts_submit(
                argv,
                label=job_id,
                cwd=cwd,
                env_extra=env_extra,
                ts_socket=self._ts_socket,
                exit_code_path=ec_path,
                stdin_file=remote_stdin_file,
            )
        except Exception as exc:
            log.error("RemoteTsRunner: failed to enqueue %s on %s: %s", job_id, self._host.name, exc)
            self._store.create(job_id)
            result = JobResult(
                ok=False,
                job_id=job_id,
                status="failed",
                tool=tool,
                tag=tag,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                summary=str(exc)[:500],
                error=ErrorBlock(
                    code="REMOTE_TS_SUBMIT_ERROR",
                    message=str(exc),
                    hint=f"Check that task-spooler (ts/tsp) is installed on {self._host.name}.",
                ),
                branch=branch,
                worktree_path=worktree_path,
            )
            self._store.write_result(result)
            return result

        self._slot_map[job_id] = slot_id
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
                "exec_host": self._host.name,
                "ts_slot": slot_id,
                "ts_ec_path": ec_path,
            },
        )

        initial = JobResult(
            ok=False,
            job_id=job_id,
            status="running",
            tool=tool,
            tag=tag,
            started_at=started_at,
            summary=(
                f"[{self._host.name}] {label}" if label else f"[{self._host.name}] queued (slot {slot_id})"
            ),
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(initial)

        t = threading.Thread(
            target=self._poll,
            args=(job_id, slot_id, ec_path, timeout_seconds, tool, tag,
                  started_at, branch, worktree_path, cleanup_fn,
                  remote_questions_dir, remote_prompt_file, remote_stdin_file),
            daemon=True,
            name=f"remote-ts-watcher-{job_id}",
        )
        t.start()
        self._watchers[job_id] = t
        return initial

    def get_result(self, job_id: str) -> JobResult | None:
        return self._store.read_result(job_id)

    def cancel(self, job_id: str) -> JobResult:
        """Mark the job cancelled and kill it on the remote ts daemon."""
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
        if result.status in _TERMINAL:
            return result

        self._cancelled.add(job_id)

        slot_id = self._slot_map.get(job_id)
        if slot_id is not None:
            try:
                self._host.ts_cancel(slot_id, ts_socket=self._ts_socket)
            except Exception as exc:
                log.warning("RemoteTsRunner: could not cancel slot %d on %s: %s", slot_id, self._host.name, exc)

        cancelled = JobResult(
            ok=False,
            job_id=job_id,
            status="cancelled",
            tool=result.tool,
            started_at=result.started_at,
            finished_at=now,
            summary=f"Cancelled by orchestrator (remote ts on {self._host.name}).",
        )
        self._store.write_result(cancelled)
        return cancelled

    def list_results(self) -> list[JobResult]:
        results: list[JobResult] = []
        for jid in self._store.list_jobs():
            r = self.get_result(jid)
            if r is not None:
                results.append(r)
        return results

    def join_all(self, timeout: float = 10.0) -> None:
        for t in list(self._watchers.values()):
            t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _ensure_slots_configured(self) -> None:
        """Set the remote ts slot count once per runner lifetime."""
        if self._slots_configured or self._max_slots is None:
            self._slots_configured = True
            return
        ts_env: dict[str, str] | None = {"TS_SOCKET": self._ts_socket} if self._ts_socket else None
        try:
            ts_bin = self._host._find_remote_ts_bin()
            self._host.run([ts_bin, "-S", str(self._max_slots)], env_extra=ts_env)
            log.debug(
                "RemoteTsRunner: set remote ts slots=%d on %s", self._max_slots, self._host.name
            )
        except Exception as exc:
            log.warning(
                "RemoteTsRunner: could not set slots on %s: %s", self._host.name, exc
            )
        self._slots_configured = True

    def _poll(
        self,
        job_id: str,
        slot_id: int,
        ec_path: str,
        timeout_seconds: int,
        tool: str,
        tag: str | None,
        started_at: datetime,
        branch: str | None,
        worktree_path: str | None,
        cleanup_fn: Callable[[], None] | None,
        remote_questions_dir: str | None,
        remote_prompt_file: str | None,
        remote_stdin_file: str | None,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        final_ts_status = "unknown"

        while time.monotonic() < deadline:
            if job_id in self._cancelled:
                return

            try:
                final_ts_status = self._host.ts_status(slot_id, ts_socket=self._ts_socket)
            except Exception as exc:
                log.warning("RemoteTsRunner: status poll failed for %s: %s", job_id, exc)
                time.sleep(self._poll_interval)
                continue

            if remote_questions_dir:
                self._sync_clarify(job_id, remote_questions_dir)

            if final_ts_status in ("finished", "failed"):
                break
            time.sleep(self._poll_interval)
        else:
            timed_out = True
            log.warning(
                "RemoteTsRunner: job %s timed out after %ds on %s",
                job_id, timeout_seconds, self._host.name,
            )
            try:
                self._host.ts_cancel(slot_id, ts_socket=self._ts_socket)
            except Exception:
                pass

        if job_id in self._cancelled:
            return
        existing = self._store.read_result(job_id)
        if existing is not None and existing.status == "cancelled":
            return

        if timed_out:
            self._write_failed(
                job_id, tool, tag, started_at,
                f"Killed after {timeout_seconds}s timeout on {self._host.name}.",
                branch, worktree_path,
            )
            if cleanup_fn is not None:
                _safe_call(cleanup_fn)
            return

        # Collect output from remote ts capture file.
        output = b""
        try:
            output = self._host.ts_output(slot_id, ts_socket=self._ts_socket)
        except Exception as exc:
            log.warning("RemoteTsRunner: could not retrieve output for %s: %s", job_id, exc)

        # Read exit code from the temp file the wrapper script wrote.
        # If ts itself reported "failed" (skipped — job never ran), trust that
        # directly rather than an ec file that may not exist.
        exit_code = -1
        if final_ts_status != "failed":
            try:
                ec_result = self._host.run(["cat", ec_path])
                exit_code = int(ec_result.stdout.decode().strip())
            except Exception:
                pass

        # Clean up remote temp files (exit-code, prompt, stdin).
        for _path in (ec_path, remote_prompt_file, remote_stdin_file):
            if _path is not None:
                try:
                    self._host.run(["rm", "-f", _path])
                except Exception:
                    pass

        # Write output to store.
        stdout_path = self._store.stdout_path(job_id)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_bytes(output)

        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        ok = exit_code == 0
        job_status: JobStatus = "completed" if ok else "failed"

        if ok:
            summary = f"Completed on {self._host.name}."
        else:
            last_lines = output[-500:].decode("utf-8", errors="replace").strip().splitlines()
            summary = last_lines[-1][:500] if last_lines else f"exit_code={exit_code}"

        result = JobResult(
            ok=ok,
            job_id=job_id,
            status=job_status,
            tool=tool,
            tag=tag,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            summary=summary,
            raw_output_ref=str(stdout_path),
            commands_run=[CommandRecord(argv=[], exit_code=exit_code, duration_ms=duration_ms)],
            branch=branch,
            worktree_path=worktree_path,
        )
        self._store.write_result(result)

        if cleanup_fn is not None:
            _safe_call(cleanup_fn)

    def _sync_clarify(self, job_id: str, remote_questions_dir: str) -> None:
        """Sync clarify_rounds Q&A files between remote and local JobStore.

        The agent (running on the remote machine) writes question files to
        *remote_questions_dir*.  This method:

        1. Discovers new ``round_NNN_questions.json`` files on the remote and
           copies them to the local ``JobStore.questions_dir``.
        2. For each round that has local answers written by the orchestrator,
           uploads ``round_NNN_answers.json`` to the remote so the agent can
           read it and continue.
        """
        local_q_dir = self._store.questions_dir(job_id)
        local_q_dir.mkdir(parents=True, exist_ok=True)

        # Discover remote question files via SSH ls.
        try:
            out = self._host.run(
                ["sh", "-c", f"ls {remote_questions_dir}/round_*_questions.json 2>/dev/null || true"]
            )
            remote_files = [p.strip() for p in out.stdout.decode().splitlines() if p.strip()]
        except Exception as exc:
            log.debug("RemoteTsRunner: clarify ls failed for %s: %s", job_id, exc)
            return

        for remote_q_path in remote_files:
            fname = remote_q_path.rsplit("/", 1)[-1]
            local_q_path = local_q_dir / fname
            if local_q_path.exists():
                continue  # already synced
            try:
                content = self._host.sftp_get(remote_q_path)
                local_q_path.write_bytes(content)
                log.debug("RemoteTsRunner: downloaded %s for job %s", fname, job_id)
            except Exception as exc:
                log.warning("RemoteTsRunner: could not download %s: %s", fname, exc)

        # Upload any locally-written answers that the remote doesn't have yet.
        try:
            local_answers = sorted(local_q_dir.glob("round_*_answers.json"))
        except Exception:
            return

        for local_a_path in local_answers:
            remote_a_path = f"{remote_questions_dir}/{local_a_path.name}"
            try:
                if self._host.sftp_exists(remote_a_path):
                    continue  # already uploaded
                self._host.sftp_put(remote_a_path, local_a_path.read_bytes())
                log.debug("RemoteTsRunner: uploaded %s for job %s", local_a_path.name, job_id)
            except Exception as exc:
                log.warning("RemoteTsRunner: could not upload %s: %s", local_a_path.name, exc)

    def _write_failed(
        self,
        job_id: str,
        tool: str,
        tag: str | None,
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
            tag=tag,
            started_at=started_at,
            finished_at=now,
            duration_ms=int((now - started_at).total_seconds() * 1000),
            summary=message[:500],
            error=ErrorBlock(
                code="REMOTE_TS_ERROR",
                message=message,
                hint=f"Check SSH connectivity and task-spooler status on {self._host.name}.",
            ),
            branch=branch,
            worktree_path=worktree_path,
        ))


def _safe_call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        pass
