# Skill: delegate

Opinionated, Claude-Code-specific patterns on top of the `unlimited-mcp` MCP
tools. Requires the `unlimited-mcp` server to be connected.

## Read the shared reference first

This skill does **not** restate the tool reference. The canonical guidance —
queue selection, sync vs background, JobResult, safety, workspace presets,
`run_command` vs `run_shell`, `run_and_summarize` vs smolagents, the timeout
multiplier table, `clarify_rounds` / `await_worker_questions`, sub-task
grouping, local-GPU agent discipline, the before-task checklist, and the
new-agent verification tests — lives in **`AGENTS.md`** at the repo root.

Read `AGENTS.md` for any of those. This file only adds the named delegation
wrappers and the Claude-Code-only behaviours below.

---

## The three delegation patterns

### `delegate.now` — sync, small tasks (< 30s)

Use when the task is short and you need the diff inline this turn. Never for
agent coding tasks (those always go background).

```
1. add_allowed_root(cwd)
2. r = delegate_to_agent(agent_name, prompt=prompt, cwd=cwd,
                          workspace='safe_dev', queue='local')
3. r = await_job(r.job_id)
4. Return r.summary and r.branch (if any)
```

### `delegate.fire_and_forget` — background, long tasks

Any agent coding task, anything > 1 min, or multiple parallel tasks. Uses
`queue="ts"` so the job survives MCP restarts and shows in the inbox.

```
1. add_allowed_root(cwd)
2. job = submit_task(agent_name=agent_name, prompt=prompt, cwd=cwd,
                      queue='ts', tag=session_tag)
3. Tell the user: "Job submitted: {job.job_id}."
4. Return immediately — recover later via list_jobs(tag=session_tag).
```

Pass a consistent `tag` (date or task name) so a new session can find the job.

### `delegate.await` — background, then report on request

```
1. add_allowed_root(cwd)
2. job = submit_task(..., queue='ts', idempotency_key=key)
3. On user request: r = get_job_result(job.job_id)
   - running   → tell the user it's still running
   - completed → show r.summary and r.diff_ref
   - failed    → show r.error and r.summary
```

Session recovery (jobs still running after a resume): `list_jobs()` for the
inbox, `list_jobs(tag=...)` to scope, `get_job_result(job_id)` marks seen.

---

## Orchestrator hard constraints

These apply to YOU (the orchestrator) at all times — no exceptions:

- **Never run bash on remote hosts** to test, fix, or review code.
- **Never fix code yourself** — always resubmit to the appropriate agent.
- **Never take over a timed-out job** — resubmit with more time.
- **Local bash is only for:** git on the main branch + `gh` CLI for PRs.
- **"Review" means** reading the agent's text output (`result.summary`,
  `result.raw_output_ref`), not opening source files yourself.

---

## Error recovery — when a job fails or stalls

**The key metric is progress, not time.** Read `raw_output_ref` first: is the
agent making forward progress, or looping on the same error?

- **Timeout with visible progress** → timeout was under-dimensioned.
  Recalculate from the `speed_tier` table in `AGENTS.md` and resubmit larger.
  Do not escalate.
- **Timeout with no progress / same error repeating** — escalate in order,
  never skip a step:
  1. Sharpen the prompt (specific files, functions, errors, acceptance
     criteria) and resubmit to the same agent.
  2. Resubmit the same task to a stronger model.
  3. Have the stronger model rewrite the affected code from scratch.

---

## Background monitoring with `ScheduleWakeup` (Claude Code only)

By default, after submitting a job you return control to the user immediately
and they ask for status when they want it.

**Use `ScheduleWakeup` only when the user explicitly asks for autonomous
follow-up** ("when it finishes, review the diff"; "run phases 1-3 in
sequence"):

```
1. Submit job → job_id
2. Tell the user: "Job running ({job_id}). I'll follow up when it finishes."
3. ScheduleWakeup(delaySeconds=120, reason="polling job {job_id}")
4. On wake-up: get_job_result(job_id)
   - running          → ScheduleWakeup again (double the interval)
   - completed/failed → do the follow-up the user asked for
```

Timing: `interval = min(270s, max(60s, expected_duration × 0.25))`. Always
stay under 300s to avoid a prompt-cache miss on every wake-up.

**Do NOT use `ScheduleWakeup` when** the user only said "submit/delegate this"
with no follow-up, the follow-up is vague ("let me know when done" — just tell
them to ask), or multiple independent jobs are running (poll them together,
don't chain a wakeup per job).

---

## Reviewing results

- `result.summary` — plain-English description (always set).
- `result.branch` — worktree branch with changes (`safe_dev`).
- `result.diff_ref` — path to the patch file.
- `result.raw_output_ref` — full stdout log (read only if needed).
- `result.risk_level` — `low` / `medium` / `high` / `critical`.
