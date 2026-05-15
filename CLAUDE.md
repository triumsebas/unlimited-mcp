# unlimited-mcp — Claude Code companion

This is the companion file for Claude Code orchestrators using the
`unlimited-mcp` MCP server.

---

## What this MCP does

`unlimited-mcp` lets you delegate work to cheaper or specialised workers
(aider, opencode, smolagents, raw commands) without blocking your context
window.  Every tool returns a **JobResult** — a structured object you can
parse for `status`, `summary`, `diff_ref`, and `raw_output_ref`.

---

## First call: `list_capabilities()`

Always call this first after connecting.  It returns what agents, providers,
and tools are configured, plus the current `allowed_roots` and safety policy.

---

## Decision tree: delegate vs do it yourself

```
Is the task > 30s or large output?  → submit_task or delegate_to_agent
Is it a coding change in a repo?    → delegate_to_agent (workspace=safe_dev)
Is it a shell command?              → run_command (or submit_task for fire-and-forget)
Is it read-only analysis?           → run_command with read-only argv
```

---

## Decision tree: sync vs background

- **`run_command`** — safety-checked, returns `status="running"` immediately.
  Poll with `get_job_result(job_id)` until status is no longer `"running"`.
- **`run_and_summarize`** — polls internally until done, then summarises via provider.
  Use for short commands where you want output digested.
- **`submit_task`** — explicit fire-and-forget.  Accepts either `argv` or
  `agent_name+prompt`.  Preferred for any job > 30s.
- **`delegate_to_agent`** — resolves an agent from config, constructs argv, submits.
  Recommended for all coding tasks.

---

## JobResult quick-ref

```json
{
  "ok": true,
  "job_id": "run_command-01J...",
  "status": "completed",          // queued | running | completed | failed | cancelled | pending_confirmation
  "summary": "Completed...",      // ≤500 chars, always populated even on failure
  "diff_ref": "/path/to/change.patch",
  "branch": "unlimited-mcp/job-...",
  "raw_output_ref": "/path/to/stdout.log",
  "error": null,
  "confirm_token": null,
  "risk_level": "low"
}
```

Read `summary` first.  Only call `get_job_result` with `raw_output_ref` if
you need the full stdout.

---

## Safety

Workers can only access paths in `allowed_roots`.  Before any repo task:

```
add_allowed_root('/path/to/target-repo')
```

Dangerous commands (e.g. `rm -rf`) return `status="pending_confirmation"` with
a `confirm_token`.  Re-call with `confirm_token=<token>` after user approval.

Shell-like argv (`bash -lc`, `python -c`) is blocked by default (`SHELL_LIKE_BLOCKED`).

---

## Workspace presets for coding tasks

| Preset | When to use |
|---|---|
| `safe_dev` | Default for any in-repo write task (git_worktree + leave_branch) |
| `quick_edit` | Small intentional edits (current dir + apply_direct) |
| `read_only` | Analysis, audits, exploration |
| `sysops_local` | Commands not tied to any repo |

Pass as `workspace="safe_dev"` to `delegate_to_agent`.

---

## Configuring agents

Use MCP tools — never edit config.yaml by hand:

```
lookup_agent_cli('aider')           # discover params and install hints
add_agent('aider_local', cli='aider', workspace='safe_dev',
          params={'model': 'gpt-4o', 'git': True})
configure_agent('aider_local', set={'model': 'claude-sonnet-4-6'})
```

Teach the server about a new CLI without restarting:
```
register_agent_knowledge('goose', command_template='goose run {prompt!q}')
add_agent('goose_local', cli='goose')
```

---

## Typical dev delegation flow

```python
# 1. Allow the target repo
add_allowed_root('/path/to/target-repo')

# 2. Delegate a coding task in an isolated worktree
result = delegate_to_agent(
    agent_name='aider_local',
    prompt='Add docstrings to all public functions in src/',
    cwd='/path/to/target-repo',
)
# result.status == 'running', result.job_id set

# 3. Poll until done
while True:
    r = get_job_result(result['job_id'])
    if r['status'] != 'running':
        break

# 4. Inspect output
# r['branch'] — the worktree branch with the changes
# r['diff_ref'] — path to the patch file
# r['summary'] — what the agent reported
```

---

## Worker clarification rounds (`clarify_rounds`)

Some tasks benefit from the agent asking questions before starting work rather
than making wrong assumptions that require a full rewrite.  Pass
`clarify_rounds=N` to `delegate_to_agent` to enable this.

```python
delegate_to_agent(
    agent_name='opencode_kimi',
    prompt='add notifications to the platform',
    clarify_rounds=1,   # 0 = no Q&A (default); 1-5 = up to N batch rounds
)
```

The agent writes all its questions at once per round to a file in the job dir,
waits for answers (max 300 s total), then proceeds.  If the job times out
waiting for an answer it exits cleanly with code 2; inspect the job dir for the
pending question and use `resume_agent_task` to relaunch with context injected.

**Use `clarify_rounds >= 1` only when ALL of these are true:**

- Design or planning task (architecture, schema, API surface, tech choices)
- OR the task is long enough that wrong assumptions would waste significant time

**Use `clarify_rounds=0` (default) when ANY of these is true:**

- Execution of commands or administrative task (no design decisions needed)
- The task is short enough that it is cheaper to relaunch than to spend time on Q&A
- The prompt already names specific files, functions, or a clear acceptance criterion

Maximum: 5 rounds, 300 s total wait.  `cleanup_jobs` removes question/answer
files together with the rest of the job directory.

---

## Phase 2 (coming)

- `run_shell(script)` — explicit shell execution with audit log
- `clarify_rounds` full implementation: `get_worker_questions`, `answer_worker_questions`, `resume_agent_task`
- `ts` task-spooler backend for durable jobs across MCP restarts
- `smart_submit` routing

---

## Claude Code–specific notes

- Long sessions are fine; submit_task keeps delegated work from blocking context.
- Use the `skills/delegate/` skill for opinionated wrappers.
- `configure_agent` persists defaults; don't just remember them in context.
