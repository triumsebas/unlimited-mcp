# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Post-job processing shared by every runner.

Runs after a coding worker exits but before the worktree is cleaned up:

1. Populate ``changed_files`` (git diff against the worktree's fork point).
2. Run the quality gate (lint + type-check) on those files, if enabled.
3. Detect file overlaps with other active/recent jobs and surface a warning.

Both the local watcher thread and the (separate-process) ts worker call
:func:`run_postprocess`; it returns native objects the local runner drops
straight into ``JobResult`` and the ts worker serialises to JSON.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from unlimited_mcp.jobs.result import JobWarning, QualityGateResult
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.workspace.git_worktree import changed_files as _git_changed_files

_ACTIVE = ("queued", "running", "pending_confirmation")
_CONFLICT_WINDOW_SECONDS = 1800  # terminal jobs newer than this are "recent"


@dataclass
class PostprocessResult:
    changed_files: list[str] = field(default_factory=list)
    quality_gate: QualityGateResult | None = None
    warnings: list[JobWarning] = field(default_factory=list)


def overlapping_jobs(
    changed_files: list[str],
    store: JobStore,
    *,
    exclude: str | None = None,
    window_seconds: int = _CONFLICT_WINDOW_SECONDS,
) -> list[dict[str, object]]:
    """Return other active/recent jobs whose ``changed_files`` overlap.

    Each entry: ``{"job_id", "status", "overlap": [files]}``. Used both by the
    proactive completion-time warning and the reactive ``detect_conflicts`` tool.
    """
    if not changed_files:
        return []
    mine = set(changed_files)
    now = datetime.now(UTC)
    hits: list[dict[str, object]] = []
    for job_id in store.list_jobs():
        if job_id == exclude:
            continue
        try:
            other = store.read_result(job_id)
        except Exception:
            continue  # half-written or invalid result.json
        if other is None or not other.changed_files:
            continue
        recent = other.status in _ACTIVE or (
            other.finished_at is not None
            and (now - other.finished_at).total_seconds() <= window_seconds
        )
        if not recent:
            continue
        overlap = sorted(mine & set(other.changed_files))
        if overlap:
            hits.append({"job_id": job_id, "status": other.status, "overlap": overlap})
    return hits


def run_postprocess(
    job_id: str,
    worktree_path: str | None,
    base_sha: str | None,
    store: JobStore,
    *,
    quality_gate_enabled: bool = True,
) -> PostprocessResult:
    """Compute changed files, run the quality gate, and detect conflicts.

    Safe to call unconditionally: with no ``worktree_path`` (e.g. a plain
    ``run_command``) it returns an empty result. Never raises — any internal
    failure degrades to a warning so it can't break job finalization.
    """
    out = PostprocessResult()
    if not worktree_path:
        return out

    try:
        out.changed_files = _git_changed_files(worktree_path, base_sha)
    except Exception as exc:  # pragma: no cover - defensive
        out.warnings.append(JobWarning(code="POSTPROCESS_DIFF_FAILED", message=str(exc)[:200]))
        return out

    if quality_gate_enabled and out.changed_files:
        with contextlib.suppress(Exception):
            from unlimited_mcp.quality import run_quality_gate

            out.quality_gate = run_quality_gate(
                worktree_path,
                out.changed_files,
                report_dir=store.artifacts_dir(job_id),
            )
            # The formatter may have touched files — refresh the list.
            with contextlib.suppress(Exception):
                out.changed_files = _git_changed_files(worktree_path, base_sha)

    with contextlib.suppress(Exception):
        hits = overlapping_jobs(out.changed_files, store, exclude=job_id)
        if hits:
            jobs_desc = "; ".join(
                f"{h['job_id']} ({len(h['overlap'])} file(s))"  # type: ignore[arg-type]
                for h in hits
            )
            out.warnings.append(
                JobWarning(
                    code="FILE_CONFLICT",
                    message=f"Changed files overlap with other recent job(s): {jobs_desc}.",
                    hint=(
                        "Call detect_conflicts() for the file-level breakdown "
                        "before merging branches."
                    ),
                )
            )
    return out
