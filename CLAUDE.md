# unlimited-mcp — Claude Code companion

This is the companion file for Claude Code orchestrators using the
`unlimited-mcp` MCP server.

The full orchestration reference — decision trees, queue selection, sync vs
background, JobResult, safety, workspace presets, timeouts, `clarify_rounds`,
sub-task grouping, agent configuration and verification — is shared with all
orchestrators and lives in **AGENTS.md**. It is imported below.

**Do not duplicate that content here. Edit AGENTS.md instead** — this file
only carries the Claude-Code-specific deltas.

@AGENTS.md

---

## Claude Code–specific notes

- Long sessions are fine: `submit_task` / `queue="ts"` keeps delegated work
  from blocking your context window. Resume later with `list_jobs()` and
  `get_job_result()`.
- Prefer the **`/delegate` skill** (`skills/delegate/SKILL.md`) for any
  multi-step delegation — it wraps these MCP tools with opinionated patterns
  and `ScheduleWakeup`-based monitoring that only exist in Claude Code.
- `configure_agent` persists defaults to config — set them there, don't just
  hold them in conversation context.
- The **`/unlimited-mcp-selftest` skill** runs the full regression checklist
  (Tier 0 lint+types, Tier A smoke tests, Tier B live end-to-end) and reports
  pass/fail per step. Use it when the user asks to run the battery or before a
  version bump. Codex has no skill; it runs the same checklist from AGENTS.md
  directly.
- In the imported reference, the **"Codex-specific notes"** section does not
  apply to you; everything else does.
