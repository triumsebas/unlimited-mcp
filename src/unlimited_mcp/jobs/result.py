# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""``JobResult`` — the universal output contract.

Every MCP tool that does work returns a ``JobResult``, sync or background.
Large outputs (stdout/stderr, diffs, artifacts) are stored on disk under
``state_dir() / "jobs" / <job_id> /`` and referenced by path; only the
small JSON envelope ever travels back to the orchestrator.

A failed job must be diagnosable in under 30 seconds without grepping
logs:

* ``summary`` is always populated (first stderr line on failure).
* The orchestrator-side helper ``get_job_result`` injects the last 2 KB of
  ``stderr.log`` into the response when ``status == "failed"``.
* Every ``ErrorBlock`` carries an actionable ``hint``; a missing hint is a
  CI failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "pending_confirmation",
]

SafetyClass = Literal["read", "mutating", "dangerous", "unknown"]
RiskLevel = Literal["low", "medium", "high", "critical"]
BlastRadius = Literal["local", "single_host", "multi_host", "external_service"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandRecord(_Strict):
    """One command executed inside a job. ``argv`` is redacted before storage."""

    argv: list[str] = Field(default_factory=list)
    cwd: str | None = None
    host: str = "local"
    exit_code: int | None = None
    duration_ms: int | None = None
    safety_class: SafetyClass = "unknown"
    risk_level: RiskLevel = "low"
    blast_radius: BlastRadius = "local"
    confirm_token_used: bool = False


class TestSummary(_Strict):
    """Parsed result from a test runner invocation, when applicable."""

    framework: str | None = None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: int | None = None
    output_ref: str | None = None


class Artifact(_Strict):
    """A named file produced by the job (logs, screenshots, csvs, ...)."""

    name: str
    path: str
    size_bytes: int = 0
    mime_type: str | None = None
    description: str | None = None


class JobWarning(_Strict):
    """Non-fatal issue surfaced by a job. Named ``JobWarning`` to avoid
    shadowing the ``Warning`` builtin."""

    code: str
    message: str
    hint: str | None = None


class QuestionRef(_Strict):
    """Reference to a worker-question file under ``jobs/<id>/questions/``."""

    id: str
    path: str
    asked_at: datetime
    answered: bool = False


class ErrorBlock(_Strict):
    """Structured error returned to the orchestrator. Never raised."""

    code: str
    message: str
    hint: str | None = None
    retryable: bool = False


QualityGateStatus = Literal["PASS", "NOPASS", "NOTDETECTED", "MISSINGDEP"]


class QualityGateIssue(_Strict):
    """One lint/type-check finding on a changed file.

    Concrete enough that the orchestrator can act without re-running anything:
    fix the file itself, or hand the issues back to the worker via
    ``resume_agent_task``.
    """

    file: str
    line: int | None = None
    col: int | None = None
    rule: str | None = None  # e.g. "F401"
    message: str
    tool: str  # "ruff" | "mypy" | "eslint" | "tsc" | "go vet" | "clippy" | ...


class QualityGateResult(_Strict):
    """Outcome of the post-job lint + type-check on the worker's changed files.

    Status meanings:

    * ``PASS``        — checks ran and the changed files are clean.
    * ``NOPASS``      — checks ran and found issues (see ``issues``).
    * ``NOTDETECTED`` — no recognized language marker among the changed files;
      the gate was skipped (not a failure).
    * ``MISSINGDEP``  — a required tool is absent from the worker PATH (see
      ``missing_deps``); the affected checks were skipped.
    """

    status: QualityGateStatus
    language: str | None = None
    tools_run: list[str] = Field(default_factory=list)
    auto_fixed: list[str] = Field(default_factory=list)
    issues: list[QualityGateIssue] = Field(default_factory=list)
    missing_deps: list[str] = Field(default_factory=list)
    report_ref: str | None = None  # path to the full raw linter output
    hint: str | None = None  # actionable next step for the orchestrator


class JobResult(_Strict):
    """Universal tool output. Sync tools complete in one call; background
    tools return ``status="queued"``/``"running"`` first and finalize via
    ``get_job_result``."""

    ok: bool
    job_id: str
    status: JobStatus
    tool: str
    tag: str | None = None  # opaque orchestrator label; filter with list_jobs(tag=...)
    started_at: datetime
    finished_at: datetime | None = None
    seen_at: datetime | None = None  # stamped by get_job_result on first read of a terminal job
    duration_ms: int | None = None

    summary: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff_ref: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    commands_run: list[CommandRecord] = Field(default_factory=list)
    tests: TestSummary | None = None
    quality_gate: QualityGateResult | None = None
    artifacts: list[Artifact] = Field(default_factory=list)

    warnings: list[JobWarning] = Field(default_factory=list)
    questions: list[QuestionRef] = Field(default_factory=list)
    raw_output_ref: str | None = None
    output_truncated: bool = False
    output_bytes: int = 0

    risk_level: RiskLevel = "low"
    blast_radius: BlastRadius = "local"

    error: ErrorBlock | None = None
    confirm_token: str | None = None
    confirm_reason: str | None = None
