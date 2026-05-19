---
name: selftest
description: >-
  Run the unlimited-mcp regression battery (Tier A automated + Tier B live
  end-to-end) and report pass/fail per step. Use when the user says "run the
  regression suite", "run the battery", "selftest", "test everything", or
  before a version bump. Requires the unlimited-mcp server connected,
  OPENCODE_API_KEY, and SSH to mcp_localhost for the Tier B steps.
---

# Skill: selftest

Claude-Code wrapper that executes the project's regression battery and
returns a single pass/fail report. It does **not** restate the test
definitions — the canonical source is **AGENTS.md → "Regression suite"**.
Read that section first; this file only adds the execution flow.

## Flow

### 1. Tier A — automated (always)

Run from the repo root:

```
uv run pytest tests/integration/test_smoke.py -q
```

- All green → continue to Tier B.
- Any failure → **stop**. Report the failing test name and the assertion.
  Do not run Tier B, do not bump version.

### 2. Tier B — live end-to-end

Execute the 6 steps in AGENTS.md → "Regression suite" → "Tier B" **in
order**, one MCP call each, verifying the stated condition before moving
on. Use `await_job` (not a polling loop). Steps 4 needs SSH to
`mcp_localhost`; step 5 needs a configured `smolagents` agent.

If a prerequisite is genuinely unavailable (no key, no SSH), mark that
step **SKIPPED (reason)** rather than FAILED, and say so explicitly in the
report — a skip is not a pass.

### 3. Report

Emit one table. Nothing else.

| Step | Result |
|---|---|
| Tier A (pytest) | ✅ / ❌ |
| B1 sysops remote | ✅ / ❌ / ⏭ |
| B2 agent local + worktree | … |
| B3 ts queue + large prompt | … |
| B4 agent remote + worktree | … |
| B5 smolagents compute | … |
| B6 run_and_summarize + clarify | … |

End with a one-line verdict: `BATTERY GREEN` only if Tier A passed and
every Tier B step is ✅ (skips are not green — call them out).

## Version-bump interaction

If this skill was invoked as part of a version bump request: only proceed
with the bump/commit/push when the verdict is `BATTERY GREEN`. Otherwise
stop and report. The full policy is in AGENTS.md → "Version-bump policy".
