# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Local subprocess backend for the ``Host`` protocol.

Executes commands in-process via :mod:`subprocess`, applying secret
redaction before any bytes touch disk.  The caller is responsible for
running the safety pipeline (:class:`~unlimited_mcp.safety.argv_check.SafetyChecker`)
*before* calling :meth:`LocalHost.run`; this class executes unconditionally.

Typical call path
-----------------
::

    decision = checker.check_run_command(argv, cwd=cwd, confirm_token=token)
    # decision.allowed is True here
    host = LocalHost(redactor=redactor)
    output = host.run(
        argv,
        cwd=cwd,
        stdout_path=store.stdout_path(job_id),
        stderr_path=store.stderr_path(job_id),
    )
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from unlimited_mcp.safety.redactor import Redactor

from .base import RunOutput


class LocalHost:
    """Run commands as local subprocesses with optional redaction and logging.

    Parameters
    ----------
    redactor:
        When provided, its :meth:`~Redactor.redact_bytes` is applied to both
        stdout and stderr *before* the bytes are written to disk or returned.
    """

    @property
    def name(self) -> str:
        return "local"

    def __init__(self, redactor: Redactor | None = None) -> None:
        self._redactor = redactor

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 60,
        output_limit_bytes: int = 1_000_000,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> RunOutput:
        env = {**os.environ, **(env_extra or {})}

        t0 = time.monotonic()
        proc = subprocess.run(
            argv,
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=timeout_seconds,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        stdout_raw: bytes = proc.stdout
        stderr_raw: bytes = proc.stderr

        if self._redactor is not None:
            stdout_raw = self._redactor.redact_bytes(stdout_raw)
            stderr_raw = self._redactor.redact_bytes(stderr_raw)

        output_bytes = len(stdout_raw) + len(stderr_raw)
        truncated = output_bytes > output_limit_bytes
        if truncated:
            # Give each stream half the budget; stderr is read first by
            # tail_output so it matters more for diagnosis.
            half = output_limit_bytes // 2
            stdout_raw = stdout_raw[-half:] if stdout_raw else b""
            stderr_raw = stderr_raw[-half:] if stderr_raw else b""

        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_bytes(stdout_raw)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_bytes(stderr_raw)

        return RunOutput(
            stdout=stdout_raw,
            stderr=stderr_raw,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            output_truncated=truncated,
            output_bytes=output_bytes,
        )
