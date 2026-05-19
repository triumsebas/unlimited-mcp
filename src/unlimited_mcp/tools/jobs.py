# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Job-management tool functions: submit_task, get_job_result, list_jobs, cancel_job,
cleanup_jobs, cleanup_branches.

These are pure Python functions — no MCP SDK import, no global state.
The server registers them as MCP tools.

Inbox semantics
---------------
``list_jobs()`` without filters returns the orchestrator's inbox: all active jobs
plus all terminal jobs whose ``seen_at`` is null.  ``get_job_result()`` is the
implicit acknowledgement — it stamps ``seen_at`` on terminal jobs the first time
they are read after reaching a terminal state.  This decouples visibility from
wall-clock time: a job left running overnight appears in the inbox the next
morning regardless of when it finished.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unlimited_mcp.jobs.result import ErrorBlock, JobResult, JobStatus
from unlimited_mcp.jobs.runner_local import LocalRunner

if TYPE_CHECKING:
    from unlimited_mcp.agents.runner import AgentRunner
    from unlimited_mcp.safety.argv_check import SafetyChecker

_TERMINAL: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})
_ACTIVE: frozenset[JobStatus] = frozenset({"queued", "running", "pending_confirmation"})


def submit_task(
    *,
    argv: list[str] | None = None,
    agent_name: str | None = None,
    prompt: str | None = None,
    label: str = "",
    tag: str | None = None,
    timeout_seconds: int = 600,
    idempotency_key: str | None = None,
    cwd: str | None = None,
    env_extra: dict[str, Any] | None = None,
    runner: LocalRunner,
    safety: SafetyChecker,
    agent_runner: AgentRunner,
) -> JobResult:
    """Submit a command or agent invocation as a background job.

    Pass either *argv* (raw command) or *agent_name* + *prompt* (agent dispatch).
    Returns immediately with ``status="running"``.

    *tag* is an opaque orchestrator-supplied label stored on the ``JobResult``.
    Use ``list_jobs(tag=...)`` to recover all jobs from a given session even after
    context loss.  The value is never interpreted by the server.

    Idempotency: if *idempotency_key* matches a non-failed existing job, that
    job's result is returned without a new submission.
    """
    if argv is None and agent_name is None:
        now = datetime.now(UTC)
        from unlimited_mcp.jobs.store import JobStore

        return JobResult(
            ok=False,
            job_id=JobStore.make_job_id("submit_task"),
            status="failed",
            tool="submit_task",
            started_at=now,
            finished_at=now,
            summary="Either argv or agent_name must be provided.",
            error=ErrorBlock(
                code="TOOL_INPUT_INVALID",
                message="Either argv or agent_name must be provided.",
                hint="Pass argv=[...] for a raw command or agent_name+prompt for an agent.",
            ),
        )

    if argv is not None:
        from unlimited_mcp.tools.execution import run_command

        return run_command(
            argv,
            safety=safety,
            runner=runner,
            cwd=cwd,
            env_extra=env_extra,
            timeout_seconds=timeout_seconds,
            tool="submit_task",
            idempotency_key=idempotency_key,
        )

    return agent_runner.submit(
        agent_name,  # type: ignore[arg-type]
        prompt=prompt,
        cwd=cwd,
        env_extra=env_extra,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
        tag=tag,
    )


def get_job_status(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Return the current status of *job_id* (read-only; does not mark as seen)."""
    result = runner.get_result(job_id)
    if result is not None:
        return result
    return _not_found(job_id)


def get_job_result(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Return the current result for *job_id*.

    **Inbox side-effect:** if the job is in a terminal state and has not been
    seen before (``seen_at`` is null), stamps ``seen_at = now()`` and persists
    the updated result.  Subsequent calls to ``list_jobs()`` (inbox view) will
    no longer include this job.

    Zombie detection is applied automatically for stale ``running`` jobs.
    """
    result = runner.get_result(job_id)
    if result is None:
        return _not_found(job_id)

    if result.status in _TERMINAL and result.seen_at is None:
        result = result.model_copy(update={"seen_at": datetime.now(UTC)})
        runner._store.write_result(result)

    return result


def await_job(
    job_id: str,
    *,
    poll_interval: float = 60.0,
    runner: LocalRunner,
) -> JobResult:
    """Block until *job_id* reaches a terminal state, then return its result.

    Polls every *poll_interval* seconds (default 60) server-side, so the
    orchestrator makes a single MCP call instead of a polling loop.
    Stamps ``seen_at`` on the result when the job completes (same as
    ``get_job_result``).
    """
    while True:
        result = runner.get_result(job_id)
        if result is None:
            return _not_found(job_id)
        if result.status in _TERMINAL:
            if result.seen_at is None:
                result = result.model_copy(update={"seen_at": datetime.now(UTC)})
                runner._store.write_result(result)
            return result
        time.sleep(poll_interval)


def list_jobs(
    *,
    runner: LocalRunner,
    tag: str | None = None,
    status: list[JobStatus] | None = None,
    include_seen: bool = False,
) -> list[JobResult]:
    """Return jobs matching the filters, defaulting to the inbox view.

    Inbox view (default, ``include_seen=False``, no *status* filter):
      - All active jobs (``running``, ``queued``, ``pending_confirmation``).
      - All terminal jobs with ``seen_at=null`` (unread).

    Pass ``include_seen=True`` for full audit history.
    Pass ``tag=...`` to scope to one orchestrator session.
    Pass ``status=[...]`` to override the default status set.
    """
    all_results = runner.list_results()
    out: list[JobResult] = []
    for r in all_results:
        if tag is not None and r.tag != tag:
            continue
        if status is not None:
            if r.status not in status:
                continue
        else:
            # Inbox filter
            if not include_seen:
                if r.status in _TERMINAL and r.seen_at is not None:
                    continue
        out.append(r)
    return out


def cancel_job(job_id: str, *, runner: LocalRunner) -> JobResult:
    """Send ``SIGTERM`` to *job_id* and mark it cancelled.

    If the job is already finished, returns the existing result unchanged.
    If the job is unknown, returns a ``JOB_NOT_FOUND`` error result.
    """
    return runner.cancel(job_id)


# ---------------------------------------------------------------------------
# Cleanup tools
# ---------------------------------------------------------------------------

def cleanup_jobs(
    *,
    runner: LocalRunner,
    older_than: str = "7d",
    keep_unseen: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Evict old job directories from disk.

    Removes the entire job directory tree, including stdout/stderr logs,
    diffs, artifacts, and any worker question/answer files under
    ``jobs/<id>/questions/``.

    Parameters
    ----------
    older_than:
        Age threshold, e.g. ``"7d"``, ``"30d"``, ``"1d"``.
    keep_unseen:
        When ``True`` (default), terminal jobs whose ``seen_at`` is null are
        spared — they are still in the orchestrator's inbox.
    dry_run:
        When ``True`` (default), returns what *would* be removed without
        actually deleting anything.  Set to ``False`` to execute.
    """
    days = _parse_duration_days(older_than)
    if dry_run:
        # Simulate without deleting
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(days=days)
        candidates: list[str] = []
        for job_id in runner._store.list_jobs():
            d = runner._store.job_dir(job_id)
            mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                continue
            if keep_unseen:
                r = runner._store.read_result(job_id)
                if r and r.status in _TERMINAL and r.seen_at is None:
                    continue
            candidates.append(job_id)
        return {"dry_run": True, "would_remove": candidates, "count": len(candidates)}

    evicted = runner._store.cleanup_older_than(days, keep_unseen=keep_unseen)
    return {"dry_run": False, "removed": evicted, "count": len(evicted)}


def cleanup_branches(
    cwd: str,
    *,
    prefix: str = "unlimited-mcp/",
    merged_into: str | None = "main",
    dry_run: bool = True,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Remove leftover ``unlimited-mcp/*`` branches from a git repository.

    These branches are created by the ``safe_dev`` workspace preset
    (``result: leave_branch``) and accumulate over time.

    When *work_dir* is provided, also removes the corresponding physical
    worktree directory from ``state/work/`` — but only if the worktree is
    clean (no uncommitted changes).  Dirty worktrees are left on disk and
    reported in ``worktrees_skipped``.

    Parameters
    ----------
    cwd:
        Path to the git repository.
    prefix:
        Only branches whose name starts with *prefix* are considered.
    merged_into:
        When set, only removes branches already merged into this ref (safe).
        Pass ``None`` to remove all matching branches regardless of merge state
        — confirm with the user before doing so.
    dry_run:
        When ``True`` (default), lists branches without deleting.
    work_dir:
        Path to ``state/work/`` where physical worktree directories live.
        Injected by the server; callers do not need to set this.
    """
    import shutil

    repo = Path(cwd)
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            return {"ok": False, "error": f"{cwd} is not a git repository."}

    # List branches with prefix
    list_cmd = ["git", "-C", str(repo), "branch", "--format=%(refname:short)"]
    proc = subprocess.run(list_cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}

    candidates = [b.strip() for b in proc.stdout.splitlines() if b.strip().startswith(prefix)]

    if merged_into:
        merged_proc = subprocess.run(
            ["git", "-C", str(repo), "branch", "--merged", merged_into, "--format=%(refname:short)"],
            capture_output=True, text=True, check=False,
        )
        merged_set = set(merged_proc.stdout.splitlines())
        candidates = [b for b in candidates if b in merged_set]

    if dry_run:
        # Report what worktrees would also be removed (clean ones only)
        worktree_preview: list[str] = []
        worktrees_dirty: list[str] = []
        if work_dir and work_dir.exists():
            for branch in candidates:
                wt_dir = work_dir / branch[len(prefix):]
                if wt_dir.is_dir():
                    if _worktree_is_clean(wt_dir):
                        worktree_preview.append(str(wt_dir))
                    else:
                        worktrees_dirty.append(str(wt_dir))
        return {
            "dry_run": True,
            "would_remove": candidates,
            "count": len(candidates),
            "worktrees_would_remove": worktree_preview,
            "worktrees_dirty_skipped": worktrees_dirty,
        }

    removed: list[str] = []
    errors: list[str] = []
    worktrees_removed: list[str] = []
    worktrees_skipped: list[str] = []

    for branch in candidates:
        del_proc = subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch],
            capture_output=True, text=True, check=False,
        )
        if del_proc.returncode != 0:
            errors.append(f"{branch}: {del_proc.stderr.strip()}")
            continue
        removed.append(branch)

        # Try to also remove the corresponding physical worktree dir
        if work_dir and work_dir.exists():
            wt_dir = work_dir / branch[len(prefix):]
            if wt_dir.is_dir():
                if _worktree_is_clean(wt_dir):
                    try:
                        shutil.rmtree(wt_dir)
                        worktrees_removed.append(str(wt_dir))
                        # Prune the now-stale registration from git
                        subprocess.run(
                            ["git", "-C", str(repo), "worktree", "prune"],
                            capture_output=True, check=False,
                        )
                    except OSError:
                        pass
                else:
                    worktrees_skipped.append(
                        f"{wt_dir} (uncommitted changes — branch removed, directory kept)"
                    )

    return {
        "dry_run": False,
        "removed": removed,
        "errors": errors,
        "count": len(removed),
        "worktrees_removed": worktrees_removed,
        "worktrees_skipped": worktrees_skipped,
    }


def _worktree_is_clean(wt_dir: Path) -> bool:
    """Return True if the git worktree at *wt_dir* has no uncommitted changes."""
    result = subprocess.run(
        ["git", "-C", str(wt_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _not_found(job_id: str) -> JobResult:
    now = datetime.now(UTC)
    return JobResult(
        ok=False, job_id=job_id, status="failed", tool="get_job_result",
        started_at=now, finished_at=now,
        summary=f"Job {job_id!r} not found.",
        error=ErrorBlock(
            code="JOB_NOT_FOUND",
            message=f"No job with id {job_id!r}.",
            hint="Call list_jobs() to see available job IDs.",
        ),
    )


def _parse_duration_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    if s.endswith("h"):
        return max(1, int(s[:-1]) // 24)
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"Cannot parse duration {s!r}. Use e.g. '7d', '30d'.")
