# unlimited-mcp — orchestrator companion (Codex / generic)

This file is for Codex CLI and generic MCP orchestrators.
Claude Code users: see CLAUDE.md instead.

---

## What this MCP does

`unlimited-mcp` lets you delegate work to cheaper or specialised workers
(aider, opencode, smolagents, raw commands) without blocking your context
window.  Every tool returns a **JobResult** — a structured object with
`status`, `summary`, `diff_ref`, and `raw_output_ref`.

---

## First call: `list_capabilities()`

Always call this first.  Returns configured agents, providers, tools,
`allowed_roots`, and the safety policy.

---

## Decision tree

```
Is the task > 30s or large output?  → submit_task (fire-and-forget)
Is it a coding change in a repo?    → delegate_to_agent (workspace=safe_dev)
Is it a shell command?              → run_command
Is it read-only analysis?           → run_command with read-only argv
```

---

## JobResult shape

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

Read `summary` first.  Only read `raw_output_ref` when you need full stdout.

---

## Safety

Workers can only access paths in `allowed_roots`.  Before any repo task:

```
add_allowed_root('/path/to/target-repo')
```

Dangerous commands return `status="pending_confirmation"` with a `confirm_token`.
Re-call with `confirm_token=<token>` to proceed.

Shell-like argv (`bash -lc`, `python -c`) is always blocked by default.

---

## Workspace presets

| Preset | When to use |
|---|---|
| `safe_dev` | Default for in-repo write tasks |
| `quick_edit` | Small intentional edits |
| `read_only` | Analysis, audits, exploration |
| `sysops_local` | Commands not tied to any repo |

---

## Codex-specific notes

- **`codex exec` is one-shot**: the session ends when the conversation ends.
  For long jobs, use `submit_task` and call `get_job_result` in a follow-up session.
- **Sandbox mode**: if Codex runs in `workspace-write` sandbox, MCP can only
  write inside that sandbox. Set `allowed_roots` narrowly to match.
- **Worker-question pattern** (phase 2) requires polling within the same session;
  in `exec` mode this only works if the job finishes before the session times out.
- Codex reads `AGENTS.md` natively; this file is the entry point.

---

## Agent configuration

```
lookup_agent_cli('aider')           # see params and install hints
add_agent('aider_local', cli='aider', workspace='safe_dev',
          params={'model': 'gpt-4o', 'git': True})
configure_agent('aider_local', set={'model': 'claude-sonnet-4-6'})
```

---

## Phase 2 (coming)

- `run_shell(script)` with explicit opt-in
- Worker questions across background jobs
- `ts` task-spooler for durable jobs across sessions
