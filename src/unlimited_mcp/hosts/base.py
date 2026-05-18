# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Host protocol — the minimal interface every execution backend must satisfy.

A ``Host`` runs a command on a specific target (local subprocess, remote
SSH, …) and returns a :class:`RunOutput`.  Redaction, safety classification,
and JobStore wiring happen *around* the Host at the caller level; the Host
itself executes unconditionally.

Phase 1 ships :class:`~unlimited_mcp.hosts.local.LocalHost` only.  The SSH
backend follows in phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RunOutput:
    """Captured result of a single command invocation.

    ``output_bytes`` is the *pre-truncation* combined size of stdout and
    stderr so callers can report accurate byte counts even when the in-memory
    buffers were trimmed.
    """

    stdout: bytes
    stderr: bytes
    exit_code: int
    duration_ms: int
    output_truncated: bool
    output_bytes: int


@runtime_checkable
class Host(Protocol):
    """Structural protocol for execution backends.

    Implementors need not declare inheritance; mypy validates conformance
    structurally.  :func:`isinstance` checks work at runtime for the method
    presence only (not signatures).
    """

    @property
    def name(self) -> str:
        """Unique, human-readable identifier for this host (e.g. ``"local"``)."""
        ...

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
        """Execute *argv* and return captured, optionally truncated output.

        Parameters
        ----------
        argv:
            Command and arguments.  ``shell=False`` always.
        cwd:
            Working directory.  ``None`` inherits from the current process.
        env_extra:
            Variables merged *on top of* the current process environment.
        timeout_seconds:
            Hard wall-clock limit.  Raises :exc:`subprocess.TimeoutExpired`
            when exceeded (callers must handle this).
        output_limit_bytes:
            Maximum combined stdout+stderr bytes retained in memory.  Bytes
            at the *head* of each stream are dropped when the limit is
            exceeded; ``output_truncated`` is set on the result.
        stdout_path / stderr_path:
            When supplied, the (already redacted, possibly truncated) bytes
            are written to these paths after the subprocess completes.
        """
        ...
