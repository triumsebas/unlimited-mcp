# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Worker clarification tools: get_worker_questions, answer_worker_questions,
resume_agent_task.

These tools manage the file-based Q&A protocol between the orchestrator and
agents running with ``clarify_rounds > 0``.  Question/answer files live inside
the job directory under ``questions/``:

    jobs/<job_id>/questions/
        round_001_questions.json   ← written by the agent (array of questions)
        round_001_answers.json     ← written by the orchestrator (array of answers)
        round_002_questions.json
        ...
        timeout.json               ← written by the agent when it gives up waiting

``get_worker_questions`` is the read side: it returns all rounds with their
questions and whether they have been answered.

``answer_worker_questions`` is the write side: it writes an answers file,
unblocking the agent.

``resume_agent_task`` handles recovery after a job exits with code 2 (timeout
waiting for answers).  It reads the full Q&A history from the failed job's
directory, builds an enriched prompt, and delegates to the same (or another)
agent with ``clarify_rounds=0`` so the agent picks up where it left off.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unlimited_mcp.jobs.result import JobResult
from unlimited_mcp.jobs.store import JobStore
from unlimited_mcp.tools.jobs import GATEWAY_SAFE_WAIT_SECONDS

if TYPE_CHECKING:
    from unlimited_mcp.agents.runner import AgentRunner
    from unlimited_mcp.jobs.runner_local import LocalRunner

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_ROUND_Q_RE = re.compile(r"^round_(\d+)_questions\.json$")
_ROUND_A_RE = re.compile(r"^round_(\d+)_answers\.json$")


# ---------------------------------------------------------------------------
# get_worker_questions
# ---------------------------------------------------------------------------


def get_worker_questions(job_id: str, *, runner: LocalRunner) -> dict[str, Any]:
    """Return all clarification rounds for a job with their answered status.

    Call this when ``list_jobs()`` shows a job still running but you want to
    check whether it is waiting for answers.  Returns an empty list when the
    job was not started with ``clarify_rounds > 0`` or has not written any
    questions yet.

    When ``pending_round`` is null and the job is still running, the agent has
    not written its questions yet.  Wait ``poll_interval_hint`` seconds before
    calling again — the agent syncs files every ~3 s, so polling faster than
    that wastes tokens without gaining information.

    ``elapsed_seconds`` and ``started_at`` reflect wall-clock time since the
    job started — use these to ground time estimates rather than counting polls.
    """
    job_result = runner._store.read_result(job_id)
    now = datetime.now(UTC)
    elapsed_seconds: float | None = None
    started_at_iso: str | None = None
    if job_result is not None and job_result.started_at is not None:
        elapsed_seconds = round((now - job_result.started_at).total_seconds(), 1)
        started_at_iso = job_result.started_at.isoformat()

    q_dir = runner._store.questions_dir(job_id)
    if not q_dir.exists():
        return {
            "job_id": job_id,
            "rounds": [],
            "pending_round": None,
            "no_questions": False,
            "timed_out": False,
            "timeout_info": None,
            "poll_interval_hint": 5,
            "elapsed_seconds": elapsed_seconds,
            "started_at": started_at_iso,
        }

    answered: set[int] = set()
    questions_by_round: dict[int, list[Any]] = {}

    for f in sorted(q_dir.iterdir()):
        m_q = _ROUND_Q_RE.match(f.name)
        m_a = _ROUND_A_RE.match(f.name)
        if m_q:
            n = int(m_q.group(1))
            try:
                questions_by_round[n] = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                questions_by_round[n] = []
        elif m_a:
            answered.add(int(m_a.group(1)))

    timeout_file = q_dir / "timeout.json"
    timed_out = timeout_file.exists()
    timeout_info: dict[str, Any] | None = None
    if timed_out:
        try:
            timeout_info = json.loads(timeout_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            timeout_info = {}

    rounds = [
        {
            "round": n,
            "questions": questions_by_round[n],
            "answered": n in answered,
            "answers_path": str(q_dir / f"round_{n:03d}_answers.json"),
        }
        for n in sorted(questions_by_round)
    ]

    # An empty round (questions == []) is the agent's explicit "I have no
    # questions, proceeding now" marker — it is NOT a pending round and must
    # never be treated as something to answer.
    pending_round = next(
        (r["round"] for r in rounds if not r["answered"] and r["questions"]),
        None,
    )
    no_questions = bool(rounds) and not any(r["questions"] for r in rounds)
    return {
        "job_id": job_id,
        "rounds": rounds,
        "pending_round": pending_round,
        # True when the agent signalled it will not ask anything and is
        # working — stop polling for questions on this job.
        "no_questions": no_questions,
        "timed_out": timed_out,
        "timeout_info": timeout_info,
        # Hint: if pending_round is set, call answer_worker_questions immediately.
        # If null and job is still running, wait this many seconds before re-polling.
        "poll_interval_hint": 0 if pending_round else 5,
        "elapsed_seconds": elapsed_seconds,
        "started_at": started_at_iso,
    }


# ---------------------------------------------------------------------------
# await_worker_questions
# ---------------------------------------------------------------------------


def await_worker_questions(
    job_id: str,
    *,
    runner: LocalRunner,
    max_wait: float = 600.0,
    poll_interval: float = 3.0,
) -> dict[str, Any]:
    """Block until a clarify-phase job needs attention, then return once.

    This replaces orchestrator-side polling for the Q&A protocol.  Make a
    single call after delegating with ``clarify_rounds > 0``; the server
    watches the job locally (the remote→local sync thread already mirrors
    questions every ~3 s) and returns as soon as **the first** of these
    happens:

    - ``outcome="questions"`` — the agent wrote a non-empty round that has no
      answers yet.  Call ``answer_worker_questions`` next.  ``pending_round``
      and ``rounds`` carry the questions.
    - ``outcome="no_questions"`` — the agent wrote an empty ``[]`` round: it
      has nothing to ask and is now working.  Do not poll again.
    - ``outcome="job_finished"`` — the job reached a terminal state without a
      pending round (it never asked, or finished).  ``status`` carries the
      final job status.
    - ``outcome="timed_out"`` — the agent wrote ``timeout.json`` (it gave up
      waiting for answers).  Use ``resume_agent_task`` to recover.
    - ``outcome="wait_expired"`` — the wait window elapsed and the agent is
      still exploring (no questions yet).  Call again to keep waiting; this
      costs one MCP round-trip, not a polling loop.

    Because waiting happens server-side, a slow worker that takes 200-600 s to
    write its first questions costs the orchestrator a single tool call, not
    dozens.  ``max_wait`` only bounds one call — re-invoking continues waiting.

    B6: each call blocks at most :data:`GATEWAY_SAFE_WAIT_SECONDS` regardless of
    ``max_wait``, to stay under the MCP gateway's ~60 s request timeout (which
    would otherwise abort the call with ``-32001`` before ``wait_expired`` could
    be returned). A larger ``max_wait`` is honoured across repeated calls.
    """
    deadline = time.monotonic() + min(max_wait, GATEWAY_SAFE_WAIT_SECONDS)
    while True:
        info = get_worker_questions(job_id, runner=runner)

        if info["pending_round"] is not None:
            return {"outcome": "questions", **info}
        if info["timed_out"]:
            return {"outcome": "timed_out", **info}
        if info["no_questions"]:
            return {"outcome": "no_questions", **info}

        job_result = runner._store.read_result(job_id)
        if job_result is not None and job_result.status in _TERMINAL_STATUSES:
            return {
                "outcome": "job_finished",
                "status": job_result.status,
                **info,
            }

        if time.monotonic() >= deadline:
            return {"outcome": "wait_expired", **info}

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# answer_worker_questions
# ---------------------------------------------------------------------------


def answer_worker_questions(
    job_id: str,
    round_number: int,
    answers: list[dict[str, Any]],
    *,
    runner: LocalRunner,
) -> dict[str, Any]:
    """Write answers for a clarification round, unblocking the waiting agent.

    Parameters
    ----------
    job_id:
        The running job whose questions you are answering.
    round_number:
        Which round to answer (1-based, matches ``round_NNN_questions.json``).
    answers:
        List of answer objects.  Each must have at minimum an ``"id"`` field
        matching the question id, plus an ``"answer"`` field with your response.
        Pass ``{"id": N, "answer": "STOP"}`` to tell the agent to proceed
        immediately with whatever it knows.

    Example
    -------
    ::

        answer_worker_questions(
            job_id="delegate_to_agent-...",
            round_number=1,
            answers=[
                {"id": 1, "answer": "B: Stateful sessions", "reasoning": "Force-logout required"},
                {"id": 2, "answer": "A: argon2id"},
            ],
        )
    """
    q_dir = runner._store.questions_dir(job_id)
    if not q_dir.exists():
        return {
            "ok": False,
            "error": f"No questions directory for job {job_id!r}. "
            "Was the job started with clarify_rounds > 0?",
        }

    q_file = q_dir / f"round_{round_number:03d}_questions.json"
    if not q_file.exists():
        return {
            "ok": False,
            "error": f"round_{round_number:03d}_questions.json not found. "
            f"Available: {[f.name for f in sorted(q_dir.iterdir())]}",
        }

    a_file = q_dir / f"round_{round_number:03d}_answers.json"
    payload = {"answered_at": datetime.now(UTC).isoformat(), "answers": answers}
    a_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "job_id": job_id,
        "round": round_number,
        "answers_written_to": str(a_file),
        "answer_count": len(answers),
    }


# ---------------------------------------------------------------------------
# resume_agent_task
# ---------------------------------------------------------------------------


def resume_agent_task(
    failed_job_id: str,
    *,
    runner: LocalRunner,
    agent_runner: AgentRunner,
    extra_context: str | None = None,
    agent_name_override: str | None = None,
    clarify_rounds: int = 0,
) -> JobResult:
    """Relaunch a clarify-phase job with the Q&A history injected.

    This is the general fallback for continuing a Q&A that the worker could
    not finish cleanly on its own.  Use it for any of:

    - The job exited with code 2 (``timeout.json``): the agent gave up waiting
      for answers, or the MCP session was interrupted mid-Q&A.
    - The agent keeps asking past the rounds you intended to grant (it wrote a
      ``round_NNN_questions.json`` beyond your budget instead of starting work).
      Cancel the running job first (``cancel_job``), then resume — optionally
      with ``clarify_rounds=1`` to grant one more controlled round, or with
      ``clarify_rounds=0`` + an ``extra_context`` of ``"proceed now"`` to force
      it to start.
    - The job reached ``completed`` but the agent only emitted a doubt or a
      comment in its output and never did the work.  Resume with the missing
      decision supplied via ``extra_context``.

    It is also the recovery path when a model simply mishandles the file
    protocol: re-stating the full Q&A history in the prompt sidesteps a worker
    that failed to read or write the question files correctly.

    The function:

    1. Reads the original invocation parameters from ``meta.json``.
    2. Reads all completed Q&A rounds from the job's ``questions/`` directory.
    3. Reads ``timeout.json`` if present (pending unanswered questions).
    4. Builds an enriched prompt: original prompt + full Q&A history + any
       ``extra_context`` you supply.
    5. Delegates to the same agent (or *agent_name_override*) with the new
       prompt and ``clarify_rounds=clarify_rounds`` (default 0 — the history
       is already in the prompt so no new Q&A phase is needed unless you want
       one).

    Parameters
    ----------
    failed_job_id:
        The job_id of the failed job to resume.
    extra_context:
        Optional free-form text appended after the Q&A history (e.g. the
        answer to the question that caused the timeout).
    agent_name_override:
        Use a different agent than the one in the original meta (e.g. a
        stronger model for a tricky follow-up).
    clarify_rounds:
        Number of additional clarification rounds for the resumed job.
        Default 0 — the context is already embedded in the prompt.
    """
    meta = runner._store.read_meta(failed_job_id)
    if meta is None:
        now = datetime.now(UTC)
        return JobResult(
            ok=False,
            job_id=JobStore.make_job_id("resume_agent_task"),
            status="failed",
            tool="resume_agent_task",
            started_at=now,
            finished_at=now,
            summary=f"No meta.json found for job {failed_job_id!r}.",
        )

    agent_name = agent_name_override or meta.get("label") or meta.get("agent_name")
    if not agent_name:
        now = datetime.now(UTC)
        return JobResult(
            ok=False,
            job_id=JobStore.make_job_id("resume_agent_task"),
            status="failed",
            tool="resume_agent_task",
            started_at=now,
            finished_at=now,
            summary="Could not determine agent name from meta.json. "
            "Pass agent_name_override explicitly.",
        )

    original_prompt: str = meta.get("original_prompt") or ""
    cwd: str | None = meta.get("cwd")
    timeout_seconds: int = int(meta.get("timeout_seconds", 600))

    # Build Q&A history section from the questions dir.
    qa_section = _build_qa_history(runner._store.questions_dir(failed_job_id))

    enriched_prompt = (
        f"{original_prompt}\n\n## Resumed session — Q&A history from previous run\n\n{qa_section}"
    )
    if extra_context:
        enriched_prompt += f"\n\n## Additional context from orchestrator\n\n{extra_context}"
    enriched_prompt += (
        "\n\nDo not repeat questions already answered above. "
        "Continue the task using the decisions already made."
    )

    return agent_runner.submit(
        agent_name,
        prompt=enriched_prompt,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        tool="resume_agent_task",
        clarify_rounds=clarify_rounds,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_qa_history(q_dir: Path) -> str:
    if not q_dir.exists():
        return "(no Q&A history found)"

    answered: dict[int, Any] = {}
    questions_by_round: dict[int, list[Any]] = {}

    for f in sorted(q_dir.iterdir()):
        m_q = _ROUND_Q_RE.match(f.name)
        m_a = _ROUND_A_RE.match(f.name)
        if m_q:
            n = int(m_q.group(1))
            try:
                questions_by_round[n] = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                questions_by_round[n] = []
        elif m_a:
            n = int(m_a.group(1))
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                answered[n] = data.get("answers", data)
            except (json.JSONDecodeError, OSError):
                answered[n] = []

    lines: list[str] = []
    for n in sorted(questions_by_round):
        lines.append(f"### Round {n}")
        for q in questions_by_round[n]:
            qid = q.get("id", "?")
            lines.append(f"**Q{qid}:** {q.get('question', '(no text)')}")
            if q.get("options"):
                for opt in q["options"]:
                    lines.append(f"  - {opt}")
        if n in answered:
            lines.append("**Answers:**")
            for a in answered[n] if isinstance(answered[n], list) else [answered[n]]:
                lines.append(
                    f"  - Q{a.get('id', '?')}: {a.get('answer', '?')}"
                    + (f" — {a['reasoning']}" if a.get("reasoning") else "")
                )
        else:
            lines.append("*(no answers received — this was the pending round)*")
        lines.append("")

    timeout_file = q_dir / "timeout.json"
    if timeout_file.exists():
        try:
            ti = json.loads(timeout_file.read_text(encoding="utf-8"))
            lines.append(f"*(agent timed out at round {ti.get('last_round', '?')})*")
        except (json.JSONDecodeError, OSError):
            lines.append("*(agent timed out)*")

    return "\n".join(lines) if lines else "(no Q&A history found)"
