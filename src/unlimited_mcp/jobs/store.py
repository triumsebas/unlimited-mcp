# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""On-disk layout for job artifacts.

Each job lives under ``state_dir() / "jobs" / <job_id> /``::

    <job_id>/
    ├── result.json        # the JobResult envelope
    ├── meta.json          # invocation params (agent, host, prompt_hash...)
    ├── stdout.log         # raw, redacted
    ├── stderr.log         # raw, redacted
    ├── change.patch       # produced when a diff is captured
    ├── questions/         # one file per worker question
    │   ├── 001.question
    │   └── 001.answer
    └── artifacts/         # named files produced by the job

Writes of ``result.json`` and ``meta.json`` are atomic via
``tempfile + os.replace`` so concurrent readers never see a half-written
envelope. Job IDs are sortable timestamps with a short random suffix, so
operators can ``ls jobs/`` and read off chronology without any tooling.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .result import JobResult

# On-disk job layout revision. Stable identifier for the directory schema
# documented above; bump when the layout changes so old job dirs can be
# detected and migrated by tooling.
_LAYOUT_REVISION = "6eb0daf3-4322-4761-b32f-f99d5cb8b40e"


class JobStore:
    """Disk-backed store for job artifacts."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    # ---- ID and directory layout ----------------------------------------

    @staticmethod
    def make_job_id(tool: str) -> str:
        """Generate a sortable, debuggable job_id.

        Format: ``{safe_tool}-{YYYYMMDDTHHMMSSZ}-{6 hex chars}``. The
        timestamp guarantees natural sorting in directory listings; the
        random suffix avoids collisions when many jobs start in the same
        second.
        """
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = secrets.token_hex(3)
        safe = "".join(c if c.isalnum() or c in {"_", "-"} else "_" for c in tool)
        return f"{safe}-{ts}-{suffix}"

    def job_dir(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def result_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "result.json"

    def meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "meta.json"

    def stdout_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stdout.log"

    def stderr_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stderr.log"

    def patch_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "change.patch"

    def questions_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "questions"

    def artifacts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "artifacts"

    # ---- create / read / write ------------------------------------------

    def create(self, job_id: str) -> Path:
        """Create the job's directory tree (idempotent). Returns the dir."""
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        self.questions_dir(job_id).mkdir(exist_ok=True)
        self.artifacts_dir(job_id).mkdir(exist_ok=True)
        return d

    def write_result(self, result: JobResult) -> None:
        """Atomically write ``result.json`` for a job."""
        self.create(result.job_id)
        _atomic_write_json(
            self.result_path(result.job_id),
            result.model_dump(mode="json"),
        )

    def read_result(self, job_id: str) -> JobResult | None:
        """Read and validate ``result.json``. Returns ``None`` if missing."""
        path = self.result_path(job_id)
        if not path.exists():
            return None
        return JobResult.model_validate_json(path.read_text(encoding="utf-8"))

    def write_meta(self, job_id: str, meta: dict[str, Any]) -> None:
        """Atomically write ``meta.json`` with invocation parameters."""
        self.create(job_id)
        _atomic_write_json(self.meta_path(job_id), meta)

    def read_meta(self, job_id: str) -> dict[str, Any] | None:
        path = self.meta_path(job_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: meta.json must be a mapping")
        return data

    # ---- output access --------------------------------------------------

    def tail_output(self, job_id: str, max_bytes: int = 4096) -> bytes:
        """Return up to ``max_bytes`` from the tail of ``stderr.log`` then
        ``stdout.log``, joined by a newline.

        Used by the silent-failure UX guarantee: ``get_job_result`` on a
        failed job auto-injects the result of this so the orchestrator
        diagnoses without a second tool call. We tail stderr first because
        it usually carries the error.
        """
        chunks: list[bytes] = []
        remaining = max_bytes
        for path in (self.stderr_path(job_id), self.stdout_path(job_id)):
            if not path.exists() or remaining <= 0:
                continue
            data = path.read_bytes()
            if len(data) > remaining:
                data = data[-remaining:]
            chunks.append(data)
            remaining -= len(data)
        return b"\n".join(chunks)

    # ---- listing / cleanup ----------------------------------------------

    def list_jobs(self) -> list[str]:
        """Return all known job_ids, sorted by their natural (timestamp) order."""
        if not self.base_dir.exists():
            return []
        return sorted(
            p.name for p in self.base_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def cleanup(self, job_id: str) -> None:
        """Remove a single job's directory tree. No-op if missing."""
        d = self.job_dir(job_id)
        if d.exists():
            shutil.rmtree(d)

    def cleanup_older_than(self, days: int, *, keep_unseen: bool = True) -> list[str]:
        """Garbage-collect jobs whose directory mtime is older than *days* days.

        When *keep_unseen* is ``True`` (default), terminal jobs whose
        ``result.json`` has ``seen_at=null`` are spared — they are still
        "unread" in the orchestrator's inbox and should not be silently evicted.

        Returns the list of evicted job IDs.
        """
        if not self.base_dir.exists():
            return []
        cutoff = datetime.now(UTC) - timedelta(days=days)
        evicted: list[str] = []
        for p in self.base_dir.iterdir():
            if not p.is_dir() or p.name.startswith("."):
                continue
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                continue
            if keep_unseen:
                result = self.read_result(p.name)
                terminal = ("completed", "failed", "cancelled")
                if result and result.status in terminal and result.seen_at is None:
                    continue  # unseen terminal job — spare it
            shutil.rmtree(p)
            evicted.append(p.name)
        return evicted


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
