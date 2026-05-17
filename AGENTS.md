# unlimited-mcp — orchestrator companion (Codex / generic)

This file is for Codex CLI and generic MCP orchestrators.
Claude Code users: see CLAUDE.md instead.

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

## Queue selection: `local` · `ts` · `ts_serial`

| Scenario | `queue=` | Why |
|---|---|---|
| Quick command, analysis, < 30s | `"local"` (default) | In-process thread, no ts dependency |
| Independent tasks, run in parallel | `"ts"` | Up to 4 simultaneous jobs; durable across restarts |
| Ordered pipeline (step 2 needs step 1 done) | `"ts_serial"` | Strictly 1 job at a time; enqueue all upfront |
| Any task expected to take > 1 min | `"ts"` or `"ts_serial"` | Don't block context; recoverable from inbox |

**Default rule: if the task is expected to take more than 2 minutes, use `"ts"` or `"ts_serial"`. No exceptions.**

- Multiple independent tasks, or single long task → `"ts"`.
- Ordered pipeline where steps don't depend on each other's output → `"ts_serial"`.
- Steps where you must read the result before deciding the next step → poll with `"local"`.
- Quick commands (< 2 min) you'll poll in the same turn → `"local"`.

When in doubt about duration, default to `"ts"`.

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
  "status": "completed",
  "summary": "...",
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

Dangerous commands return `status="pending_confirmation"` with a `confirm_token`.
Re-call with `confirm_token=<token>` after user approval.

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

## run_command vs run_shell

`run_command` takes an argv list. Use it for any known command:
`run_command(argv=["git", "log", "--oneline", "-10"])`.

`run_shell` takes a script string and passes it to bash. Use it **only** when
you need shell features that argv can't express:

| Need | Use |
|---|---|
| Simple command | `run_command` |
| Pipes: `grep ERROR log \| sort \| uniq -c` | `run_shell` |
| Redirections: `cmd > out.txt 2>&1` | `run_shell` |
| Loops, expansions, chained steps | `run_shell` |

`run_shell` always requires `i_understand_this_runs_a_shell_script=True`.

---

## run_and_summarize vs smolagents

| Criterion | `run_and_summarize` | smolagents |
|---|---|---|
| Single shell command | ✓ ideal | ✓ (more overhead) |
| Process/transform output with Python | ✗ | ✓ |
| Multiple chained steps | ✗ | ✓ |
| File operations or calculations | ✗ | ✓ |

Use `run_and_summarize` when the task is literally one known command and you
just need a summary. Use `delegate_to_agent(agent="smolagents_opencode", ...)`
when you need logic, chained steps, or computation.

---

## Timeout guide

**`timeout_seconds` is execution time only** — starts when the worker actually
runs, not when the job was queued. Always overestimate.

Step 1 — estimate how long the task would take for Claude (baseline).
Step 2 — look up the agent's `speed_tier` from `list_capabilities()`.
Step 3 — apply the multiplier:

| `speed_tier` | Multiplier | Typical backend |
|---|---|---|
| `fast` | 1× | Claude or equivalent |
| `acceptable` | 2–3× | API-backed LLM (deepseek, qwen, gemini…) |
| `slow` | 10–20× | Local GPU (MLX, llama.cpp) |
| `unusable` | 50×+ | Local CPU — only for tiny tasks |

| Task type | Claude | `acceptable` (3×) | `slow` (15×) |
|---|---|---|---|
| Docstrings / quick refactor | 450 s | 1 350 s | 6 750 s |
| New feature / test suite | 900 s | 2 700 s | 13 500 s |
| Complex multi-file task | 3 600 s | 10 800 s | 54 000 s |

---

## clarify_rounds — Q&A before the worker starts

Pass `clarify_rounds=N` to `delegate_to_agent` to let the agent ask questions
before writing any code.  The Q&A protocol is injected automatically — just
pass the number.

```python
result = delegate_to_agent(
    agent_name='opencode_pro',
    prompt='Add a notification system to the platform',
    clarify_rounds=2,       # 0 = no Q&A (default); 1-5 = up to N rounds
    cwd='/path/to/repo',
    workspace='safe_dev',
)
```

After any `clarify_rounds >= 1` delegation, make **one** blocking call:

```python
res = await_worker_questions(result['job_id'])
```

Act on `res['outcome']`:

| outcome | meaning | next action |
|---|---|---|
| `"questions"` | agent asked; `pending_round`/`rounds` carry them | `answer_worker_questions(job_id, round, answers)` |
| `"no_questions"` | agent wrote `[]` — nothing to ask, working | do nothing, wait for job |
| `"job_finished"` | finished without asking | review as normal |
| `"timed_out"` | agent gave up waiting for answers | `resume_agent_task` |
| `"wait_expired"` | still exploring; `max_wait` elapsed | call `await_worker_questions` again |

**Do NOT poll `get_worker_questions` in a loop.** `await_worker_questions` blocks
server-side and returns once, regardless of how long the worker takes to explore.

**When to use `clarify_rounds`:**

- Use `clarify_rounds >= 1` for design/planning tasks or long tasks where wrong
  assumptions are costly.
- Use `clarify_rounds=0` (default) for mechanical tasks with fully-specified
  prompts naming exact files, functions, or acceptance criteria.
- `timeout_seconds` is auto-extended by `clarify.max_total_seconds` (600 s)
  when `clarify_rounds > 0` — do not add it manually.

---

## Grouping sub-tasks in one delegation

Each delegation is a fresh session — the worker re-explores the repo from
scratch.  Bundling amortises that cost, but only when sub-tasks share context.
Decision checklist (in priority order):

1. Same files or module → bundle.
2. Sequential dependency (B needs A's output) → bundle and number them.
3. Same conceptual domain (all "logging", all "auth") → bundle.
4. None of the above → separate delegations, run in parallel.

Cap at ~3-4 sub-tasks; the combined diff must be reviewable in one sitting.
Never bundle by convenience — a timeout or wrong assumption kills the whole lot.

---

## Local-GPU agents (`speed_tier` slow/unusable)

Applies **only** to agents whose model runs on a local GPU (user's choice —
privacy or cost).  Not to be confused with agents named "local" that proxy to
a remote cloud API.

- Always `clarify_rounds=0`.  Resolve ambiguity in the prompt yourself.
- Name exact files or directories the agent must read; do not let it scan the
  whole repo.
- Write an explicit acceptance criterion so the agent stops at the right point.

---

## Before any task checklist

1. `list_capabilities()` — confirm the target agent is configured.
2. `add_allowed_root(cwd)` — grant the agent access to the repo.
3. If agent not configured: `lookup_agent_cli(cli)` → `add_agent(...)`.

---

## Typical delegation flow

```python
# 1. Allow the target repo
add_allowed_root('/path/to/target-repo')

# 2. Delegate a coding task in an isolated worktree
result = delegate_to_agent(
    agent_name='aider_local',
    prompt='Add docstrings to all public functions in src/',
    cwd='/path/to/target-repo',
    workspace='safe_dev',
    queue='ts',
)

# 3. Wait until done — prefer await_job (single blocking call) over polling
r = await_job(result['job_id'])
# or if you need manual control: poll get_job_result(job_id) until status != 'running'

# 4. Inspect
# r['branch']      — worktree branch with changes
# r['diff_ref']    — path to the patch file
# r['summary']     — what the agent reported
```

---

## Session recovery

If you resume and need to find in-flight or unread jobs:

```
list_jobs()                        # inbox: active + unseen terminal
list_jobs(tag='my-session-tag')    # scope to a specific session
get_job_result(job_id)             # marks terminal job as seen
```

---

## Agent configuration

```
lookup_agent_cli('aider')           # see params and install hints
add_agent('aider_local', cli='aider', workspace='safe_dev',
          params={'model': 'gpt-4o', 'git': True})
configure_agent('aider_local', set={'model': 'claude-sonnet-4-6'})
```

---

## Verifying a new coding agent

Run these three tests before marking an agent as `verified: true` in knowledge.yaml.
All three use `workspace="none"` and `add_allowed_root("/tmp")` first.

**Test 1 — inline prompt**
```python
delegate_to_agent(agent_name, prompt='Say exactly: "<agent> ok"',
                  workspace="none", timeout_seconds=60)
```

**Test 2 — large prompt (> 64 KB, triggers stdin/file fallback)**
```python
large_prompt = 'Say exactly: "<agent> file-prompt OK"\n' + 'x' * 70000
delegate_to_agent(agent_name, prompt=large_prompt,
                  workspace="none", timeout_seconds=60)
```

**Test 3 — file write + delete in /tmp**
```python
delegate_to_agent(agent_name,
    prompt='Create /tmp/<agent>_test.txt with content "<agent>-write OK", '
           'then delete it, then say "<agent> file-write OK"',
    workspace="none", timeout_seconds=90)
```
If the agent hangs waiting for a permission prompt, add `--yolo` /
`--dangerously-skip-permissions` to its `command_template` in knowledge.yaml.

---

## Codex-specific notes

- **`codex exec` is one-shot**: the session ends when the conversation ends.
  For long jobs, use `submit_task` with `queue="ts"` and call `get_job_result`
  in a follow-up session.
- **Sandbox mode**: if Codex runs in `workspace-write` sandbox, MCP can only
  write inside that sandbox. Set `allowed_roots` narrowly to match.
- **Worker questions**: use `await_worker_questions(job_id)` — one blocking
  call, not a polling loop.  In `exec` mode this only works if the job
  resolves (questions appear or job finishes) before the session times out.
- Codex reads `AGENTS.md` natively; this file is the entry point.
