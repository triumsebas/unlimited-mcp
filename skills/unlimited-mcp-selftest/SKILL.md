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

**Safety rule: never use the user's repos as test targets.** All coding
agent tests (B2, B4, B6b) use throwaway git repos created in `/tmp` and
deleted immediately after.

---

## Preparation (before Tier B)

Create two throwaway git repos — one local, one on the remote:

```bash
# local throwaway repo
mkdir -p /tmp/umcp-selftest
cd /tmp/umcp-selftest
git init
echo 'def hello():\n    pass' > hello.py
git add hello.py
git commit -m "init"
```

```bash
# remote throwaway repo (via run_command on mcp_localhost)
run_command(["git", "init", "/tmp/umcp-selftest"], exec_host="mcp_localhost")
run_command(["git", "-C", "/tmp/umcp-selftest", "commit", "--allow-empty", "-m", "init"],
            exec_host="mcp_localhost")
# write hello.py on remote via a small echo chain or sftp
```

Also: `add_allowed_root("/tmp/umcp-selftest")` so the agent can access it.

---

## Flow

### 0. Tier 0 — Lint & types (headless, always)

Run from the **unlimited-mcp repo root** (not the throwaway repo):

```
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

All green → continue. Any failure → **stop**, report output.

### 1. Tier A — Automated (always)

```
uv run pytest tests/integration/test_smoke.py -q
```

All green → continue. Any failure → **stop**, report failing test + assertion.

### 2. Tier B — Live end-to-end

**B1 — Sysops remote**
```python
r = run_command(["echo", "b-remote"], exec_host="mcp_localhost")
r = await_job(r["job_id"])
```
Pass: `status == "completed"` and stdout contains `b-remote`.

**B2 — Agent local + worktree** (uses `/tmp/umcp-selftest`)
```python
r = delegate_to_agent(
    "opencode_flash",
    prompt='Add a one-line docstring to the hello() function in hello.py. Only change that file.',
    cwd="/tmp/umcp-selftest",
    workspace="safe_dev",
    timeout_seconds=180,
)
r = await_job(r["job_id"])
```
Pass: `status == "completed"`, `branch` non-null, `changed_files` non-empty.

**B3 — ts queue + large prompt**
```python
large = 'Say exactly: "b-ts OK"\n' + "x" * 70000
r = delegate_to_agent("opencode_flash", prompt=large,
                       workspace="none", queue="ts", timeout_seconds=120)
r = await_job(r["job_id"])
```
Pass: `status == "completed"` and `"b-ts OK"` in `r["summary"]`.

**B4 — Agent remote + worktree** (uses `/tmp/umcp-selftest` on remote)
```python
r = delegate_to_agent(
    "opencode_ssh_flash",
    prompt='Add a one-line docstring to the hello() function in hello.py. Only change that file.',
    cwd="/tmp/umcp-selftest",
    workspace="safe_dev",
    exec_host="mcp_localhost",
    timeout_seconds=180,
)
r = await_job(r["job_id"])
```
Pass: `status == "completed"` and `branch` non-null.

**B5 — smolagents compute**
```python
r = delegate_to_agent(
    "smolagents_opencode",
    prompt='Given {"a":1,"b":2}, write and run code that prints the sum of the values',
    workspace="none",
    timeout_seconds=120,
)
r = await_job(r["job_id"])
```
Pass: `status == "completed"` and `"3"` in `r["summary"]`.

**B6a — run_and_summarize**
```python
r = run_and_summarize(["echo", "summarize-me"])
```
Pass: `status == "completed"`.

**B6b — clarify round** (uses `/tmp/umcp-selftest`)
```python
r = delegate_to_agent(
    "opencode_flash",
    prompt='Add a one-line docstring to the hello() function in hello.py. Only change that file.',
    cwd="/tmp/umcp-selftest",
    workspace="safe_dev",
    clarify_rounds=1,
    timeout_seconds=300,
)
res = await_worker_questions(r["job_id"])
# outcome "no_questions" or "questions" are both fine — agent either
# proceeded directly or asked; answer any questions then await_job.
if res["outcome"] == "questions":
    answer_worker_questions(r["job_id"], res["pending_round"],
                            [{"id": q["id"], "answer": "STOP"} for q in res["rounds"][-1]["questions"]])
r = await_job(r["job_id"])
```
Pass: `status == "completed"`.

---

## Cleanup (after Tier B)

```bash
rm -rf /tmp/umcp-selftest
run_command(["rm", "-rf", "/tmp/umcp-selftest"], exec_host="mcp_localhost")
```

---

## Report

Emit one table. Nothing else.

| Step | Result |
|---|---|
| 0a ruff | ✅ / ❌ |
| 0b mypy | ✅ / ❌ |
| Tier A pytest | ✅ / ❌ |
| B1 sysops remote | ✅ / ❌ / ⏭ SKIP |
| B2 agent local + worktree | ✅ / ❌ / ⏭ SKIP |
| B3 ts + large prompt | ✅ / ❌ / ⏭ SKIP |
| B4 agent remote + worktree | ✅ / ❌ / ⏭ SKIP |
| B5 smolagents compute | ✅ / ❌ / ⏭ SKIP |
| B6a run_and_summarize | ✅ / ❌ / ⏭ SKIP |
| B6b clarify round | ✅ / ❌ / ⏭ SKIP |

End with: **`BATTERY GREEN`** only if Tier 0 + Tier A passed and every
Tier B step is ✅. Skips are not green — state reason explicitly.

## Version-bump policy

Only bump version/commit/push when verdict is `BATTERY GREEN`. Full policy
in AGENTS.md → "Version-bump policy".
