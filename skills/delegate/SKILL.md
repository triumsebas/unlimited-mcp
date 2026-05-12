# Skill: delegate

Opinionated wrappers around the `unlimited-mcp` MCP tools for common
delegation patterns.  Requires the `unlimited-mcp` server to be connected.

---

## delegate.now — sync delegation (small tasks)

For quick tasks where you want to wait for the result in the same turn.

```
1. add_allowed_root(cwd)
2. result = delegate_to_agent(agent_name, prompt=prompt, cwd=cwd, workspace='safe_dev')
3. Poll: while result.status == 'running': result = get_job_result(result.job_id)
4. Return result.summary and result.branch (if applicable)
```

Use when: task is < 2 min, you need the diff inline to review.

---

## delegate.fire_and_forget — background delegation

For tasks > 30s or when you want to continue working.

```
1. add_allowed_root(cwd)
2. job = submit_task(agent_name=agent_name, prompt=prompt, cwd=cwd)
3. Tell user: "Job submitted: {job.job_id}. Check back with get_job_result({job.job_id})."
4. Return immediately.
```

Use when: task will take minutes, or multiple parallel tasks.

---

## delegate.await — fire-and-forget then await on request

```
1. add_allowed_root(cwd)
2. job = submit_task(agent_name=agent_name, prompt=prompt, cwd=cwd, idempotency_key=key)
3. When user asks for results: result = get_job_result(job.job_id)
4. If result.status == 'running': tell user it's still running.
5. If result.status == 'completed': show result.summary and result.diff_ref.
6. If result.status == 'failed': show result.error and result.summary.
```

---

## Workspace selection guide

| Task type | workspace preset |
|---|---|
| Write code in a repo | `safe_dev` (default) |
| Quick in-place edit | `quick_edit` |
| Read/analyse only | `read_only` |
| Shell commands (no repo) | `sysops_local` |

---

## Before any task checklist

1. `list_capabilities()` — confirm the target agent is configured.
2. `add_allowed_root(cwd)` — grant the agent access to the repo.
3. If agent not configured: `lookup_agent_cli(cli)` → `add_agent(...)`.

---

## Reviewing results

- `result.summary` — plain-English description of what happened (always set).
- `result.branch` — worktree branch with changes (for `safe_dev`).
- `result.diff_ref` — path to the patch file.
- `result.raw_output_ref` — path to full stdout log (read only if needed).
- `result.risk_level` — `low` / `medium` / `high` / `critical`.
