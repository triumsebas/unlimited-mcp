---
name: unlimited-mcp-selftest
description: >-
  Run the unlimited-mcp regression battery (Tier 0 lint+types, Tier A
  automated, Tier B live end-to-end) and report pass/fail per step. Use
  when the user says "run the regression suite", "run the battery",
  "selftest", "test everything", or before a version bump. Requires the
  unlimited-mcp server connected, OPENCODE_API_KEY, and SSH to
  mcp_localhost for the Tier B steps.
---

# Skill: unlimited-mcp-selftest

Claude-Code wrapper that executes the project's regression battery and
returns a single pass/fail report. The canonical test definitions live in
**AGENTS.md → "Regression suite"**; this file only adds the execution flow.

Abort as soon as any tier fails — do not proceed to the next one.

## Flow

### 0. Tier 0 — Lint & types (headless, always)

Run from the repo root:

```
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

- All green → continue to Tier A.
- Any failure → **stop**. Report the output. Do not run Tier A or Tier B.

### 1. Tier A — Automated (always)

```
uv run pytest tests/integration/test_smoke.py -q
```

- All green → continue to Tier B.
- Any failure → **stop**. Report the failing test name and assertion.
  Do not run Tier B, do not bump version.

### 2. Tier B — Live end-to-end

Execute the 6 steps in AGENTS.md → "Regression suite" → "Tier B" **in
order**, one MCP call each, verifying the stated condition before moving
on. Use `await_job` (not a polling loop). Step B4 needs SSH to
`mcp_localhost`; step B5 needs a configured `smolagents` agent.

If a prerequisite is genuinely unavailable (no key, no SSH), mark that
step **SKIPPED (reason)** rather than FAILED, and say so explicitly in the
report — a skip is not a pass.

### 3. Report

Emit one table. Nothing else.

| Step | Result |
|---|---|
| 0a ruff | ✅ / ❌ |
| 0b mypy | ✅ / ❌ |
| Tier A (pytest) | ✅ / ❌ |
| B1 sysops remote | ✅ / ❌ / ⏭ |
| B2 agent local + worktree | … |
| B3 ts queue + large prompt | … |
| B4 agent remote + worktree | … |
| B5 smolagents compute | … |
| B6 run_and_summarize + clarify | … |

End with a one-line verdict: `BATTERY GREEN` only if all tiers passed.
Skips are not green — call them out explicitly.

## Version-bump interaction

If this skill was invoked as part of a version bump request: only proceed
with the bump/commit/push when the verdict is `BATTERY GREEN`. Otherwise
stop and report. The full policy is in AGENTS.md → "Version-bump policy".
