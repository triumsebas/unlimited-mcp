# Dev demo — orchestrator delegates a coding task

This demo shows Claude Code (or any MCP orchestrator) delegating a coding
task to a local agent via `unlimited-mcp`, with full workspace isolation.

The target file is `sample.py` — it has public functions with no docstrings.
The agent's job: add docstrings to all public functions.

---

## Prerequisites

1. `unlimited-mcp` installed and the MCP server running.
2. At least one agent configured (e.g. `aider_local`).
3. This repo directory known to the orchestrator.

---

## Step-by-step

Run these MCP tool calls from your Claude Code session (or any orchestrator):

### 1. Check what's available

```
list_capabilities()
```

You should see at least one agent under `agents`. If empty, continue to step 1b.

### 1b. (If no agent configured) Add aider

```
lookup_agent_cli('aider')                    # see params and install hint
add_agent('aider_local', cli='aider',
          workspace='safe_dev',
          params={'git': True, 'auto_commits': False})
```

### 2. Grant filesystem access to this demo directory

```
add_allowed_root('/path/to/unlimited-mcp/demos/dev')
```

Replace with the actual path on your machine.

### 3. Delegate the task

```
delegate_to_agent(
    agent_name='aider_local',
    prompt='Add a one-line docstring to every public function and method in sample.py. Do not change any logic.',
    cwd='/path/to/unlimited-mcp/demos/dev',
)
```

This returns immediately with `status='running'` and a `job_id`.

### 4. Poll for completion

```
get_job_result('<job_id>')
```

When `status='completed'`:
- `result.branch` — the git worktree branch with the changes
- `result.diff_ref` — path to the `.patch` file
- `result.summary` — what the agent reported

### 5. Review the diff

```bash
cat <diff_ref>
```

The original `sample.py` in this directory is untouched.  All changes are
on the agent's worktree branch.  To apply:

```bash
git merge <branch>
```

Or discard:

```bash
git worktree remove --force <worktree_path>
git branch -D <branch>
```

---

## What this demonstrates

- `add_allowed_root` gates filesystem access from the orchestrator side.
- `delegate_to_agent` with `workspace='safe_dev'` creates a git worktree so
  the agent never touches the main working tree.
- The orchestrator stays responsive while the agent works in the background.
- `get_job_result` gives a structured summary without inlining raw output.
