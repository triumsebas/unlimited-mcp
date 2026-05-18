# Architecture

How `unlimited-mcp` is wired internally. For day-to-day usage see the
[README](README.md); for the orchestration tool reference see
[AGENTS.md](AGENTS.md).

In this project the frontier model (Claude Code, Codex) is the
**architect / orchestrator** and the delegated agents are the
**workers / contractors**. The rest of this document uses *orchestrator* and
*worker* (or *agent*).

---

## Component map

```
                   ┌─────────────────────────────────────┐
                   │         MCP Tool Interface           │
                   │  delegate_to_agent · run_command     │
                   │  submit_task · get_job_result        │
                   │  add_agent · add_provider            │
                   │  list_capabilities · query_logs      │
                   └──────────────┬──────────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
     ┌────────────────┐  ┌───────────────┐  ┌─────────────────┐
     │  Agent Runner  │  │  Config Store │  │ Safety Checker  │
     │  (resolves CLI │  │  (conversatio-│  │ (path allowlist │
     │  builds argv)  │  │  nally driven)│  │  argv blocking) │
     └───────┬────────┘  └───────────────┘  └─────────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
┌──────────┐   ┌──────────────────────────────────┐
│  Local   │   │  Task Spooler (ts) backend        │
│  Runner  │   │  • Durable: survives MCP restart  │
│  (async) │   │  • Queues: local · ts · ts_serial │
└────┬─────┘   └──────────────┬───────────────────┘
     │                        │
     └──────────┬─────────────┘
                │
     ┌──────────┴───────────────────────────────┐
     ▼                    ▼                     ▼
┌──────────────┐  ┌───────────────┐   ┌─────────────────────┐
│  Workspace   │  │  Agent CLIs   │   │  Direct LLM queries │
│  Manager     │  │  aider        │   │  research · analysis│
│  git worktree│  │  opencode     │   │  no local exec      │
│  per job     │  │  claude · pi  │   │  needed             │
└──────────────┘  │  goose · ...  │   └──────────┬──────────┘
                  └──────┬────────┘              │
                         │              ┌─────────┴──────────┐
                         ▼              ▼                    ▼
                  ┌─────────────┐  Remote APIs         Local inference
                  │ System cmds │  OpenAI · Anthropic  Ollama · MLX
                  │ scripts     │  Groq · OpenRouter   LM Studio
                  │ automation  │  (any OpenAI-compat) (zero API cost)
                  └─────────────┘
```

---

## JobResult

Every tool returns a structured result so the orchestrator never has to parse
raw stdout unless it wants to:

```json
{
  "ok": true,
  "job_id": "delegate-01J...",
  "status": "completed",
  "summary": "Added type hints to 47 functions across 12 files.",
  "branch": "unlimited-mcp/job-01J...",
  "diff_ref": "/path/to/changes.patch",
  "raw_output_ref": "/path/to/stdout.log"
}
```

Read `summary` first. Only follow `raw_output_ref` when you need the full log,
and `diff_ref` / `branch` to review or merge a coding job's changes.
