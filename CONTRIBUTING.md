# Contributing to unlimited-mcp

Thank you for your interest. This project is early-stage and any contribution helps.

## What's most needed right now

- **New agent CLIs** — if you use a coding agent not listed in `knowledge.yaml`, add it (see below)
- **SSH backend** (Phase 3) — the module skeleton exists in `src/unlimited_mcp/backends/ssh/`; paramiko is already an optional dependency
- **Tests** — unit tests under `tests/unit/`, integration tests under `tests/integration/`
- **Bug reports** — open an issue with the output of `query_logs()` and your config (redact API keys)

## Development setup

```bash
git clone https://github.com/triumsebas/unlimited-mcp.git
cd unlimited-mcp
uv sync --all-extras
```

### Symlink the delegate skill (avoid two copies)

If you use Claude Code with the `unlimited-mcp` skill installed **and** you are also developing the server in this repo, your installed skill (`~/.claude/skills/delegate/SKILL.md`) and the repo copy (`skills/delegate/SKILL.md`) will drift apart unless you keep them in sync.

The simplest fix is a symlink so there is only one file:

```bash
# Run once after cloning; replace the path with your actual clone location
ln -sf /path/to/unlimited-mcp/skills/delegate/SKILL.md \
       ~/.claude/skills/delegate/SKILL.md
```

After this, every edit to `skills/delegate/SKILL.md` is immediately live in your Claude Code session — no copy step needed.

---

### Hot-reload development (dogfooding)

`uv sync` installs the package **editable** into the repo's venv, so the
running server *is* the source you're editing. Point your MCP client at the
repo checkout instead of a global install:

```json
{
  "mcpServers": {
    "unlimited-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/unlimited-mcp", "unlimited-mcp"]
    }
  }
}
```

Now you can use the MCP and fix its code in the same session: edit a file,
then call the `restart_server` tool (or `install_and_restart` if you changed
dependencies) and the orchestrator reconnects to the patched server without
leaving the conversation. This is how the project is developed — bugs found
mid-task are fixed and reloaded in place.

Run tests:

```bash
# Unit tests only (no external tools needed)
uv run pytest tests/unit/

# Include integration tests (requires aider, opencode, ts on PATH)
uv run pytest -m "not requires_opencode_key"
```

Lint and type-check:

```bash
uv run ruff check src/
uv run mypy src/
```

## Adding a new agent CLI

The quickest contribution is adding a new agent to `knowledge.yaml`. Each entry needs:

```yaml
agents:
  your_agent:
    description: "One-line description of what it does"
    install_hint: "pip install your-agent-cli"
    docs_url: "https://docs.your-agent.com"
    command_template: "your-agent {prompt!q}"   # {prompt!q} = shell-quoted prompt
    prompt_delivery: "arg"                       # arg | stdin | arg_with_stdin_fallback
    params:
      model:
        default: "provider/model-name"
        description: "LLM model to use"
    verified: false
```

Test it with `lookup_agent_cli('your_agent')` after adding.

## Pull requests

- Keep PRs focused — one feature or fix per PR
- Add or update tests for any changed behavior
- `ruff` and `mypy` must pass (CI checks both)
- For larger features, open an issue first to discuss approach

## Code style

- Python 3.11+, async-first
- Pydantic models for all structured data
- structlog for all logging (never `print()`)
- No shell-like subprocesses from Python — use the `SafetyChecker` pattern

## Reporting issues

Include:
- Output of `list_capabilities()` (redact API keys and secrets)
- Relevant output from `query_logs(level='error', limit=20)`
- Your OS and Python version
