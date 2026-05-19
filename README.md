<div align="center">

<img src="assets/hero.svg" alt="unlimited-mcp — Tired of burning your limits? Go unlimited." width="900"/>

[![Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/unlimited-mcp)](https://pypi.org/project/unlimited-mcp/)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)](https://github.com/triumsebas/unlimited-mcp/releases)

</div>

[Why](#why-unlimited-mcp) • [Quick start](#quick-start) • [Use cases](#use-cases) • [Architecture](#architecture) • [Roadmap](#roadmap) • [Contributing](#contributing)

> **Stop burning your AI limits.** An MCP server that delegates coding and sysops work to any cheaper agent or local LLM via durable background queues — git-worktree isolation, safety policies, and clarification rounds included.

---

## What it does

`unlimited-mcp` is an **MCP server** that sits between your frontier orchestrator (Claude Code, Codex, etc.) and a pool of cheaper workers (local agents, local LLMs, background processes). It exposes a clean set of MCP tools that let your AI:

- **Delegate coding tasks** to aider, opencode, or other agent CLIs running cheaper models
- **Run any command, script, or automation** on your local system or a remote server
- **Run background jobs** that survive your session ending — even closing Claude Code
- **Use local or remote GPU** via Ollama, MLX, or LM Studio at zero API cost
- **Manage git workspaces** — worktrees, commits, and merges are handled automatically
- **Stay in control** — safety checks, path allowlists, and an audit log keep things safe

In the terms people search for: your frontier model (Claude Code, Codex) is the **architect / orchestrator**; the delegated agents are the **workers / contractors**. The rest of this README uses *orchestrator* and *worker* (or *agent*).

```
  Claude Code          Codex
  Opus · Sonnet        gpt-5.5
       │                  │
       └────────┬─────────┘
                │ MCP tools
                ▼
  ┌─────────────────────────┐
  │    unlimited-mcp        │
  │  ┌───────────────────┐  │
  │  │   Safety Layer    │  │  path allowlists · argv checks · audit log
  │  ├───────────────────┤  │
  │  │ Workspace Manager │  │  git worktrees · branches · merges
  │  ├───────────────────┤  │
  │  │   Job Runners     │  │  local async · Task Spooler (durable)
  │  └───────────────────┘  │
  └──────────┬──────────────┘
             │
    ┌────────┼────────────────┐
    ▼        ▼                ▼
Agent CLIs  Local LLMs    System Tools
aider       Ollama · MLX  commands · scripts
opencode    LM Studio     shell · automation
claude      (zero cost)   sysops tasks
jcode
goose
```

---

## The problem that built this

Subscriptions burn fast. Claude's 5-hour limit, Codex credits, API costs — anyone doing serious AI-assisted development hits walls constantly.

**This project was built using itself.** Here's what happened:

> Same project, same plan:
>
> - **Without this tool:** Opus designed, Sonnet implemented — hitting the 5-hour limit repeatedly over several days, and eventually the weekly limit too.
> - **With this tool:** Sonnet orchestrated opencode Go, delegating the hard work and fixes to DeepSeek V3 Flash (with DeepSeek V3 Pro doing critical reviews). Finished in a single sitting. Not a single 5-hour limit hit. opencode Go never came close to its subscription limit either.

The insight: **you don't need the best model for everything.** You decide what gets delegated, to which agent, running which model — the orchestrator just executes your strategy. Design and supervision stay with the expensive model; everything else goes wherever you point it.

How much you save isn't a fixed number — it scales with *your* strategy: the more you delegate and the less you need the frontier model to do hands-on, the more limits and cost you reclaim. You set the dial between "Claude does everything" and "Claude only designs and reviews."

**`unlimited-mcp` is the infrastructure that makes this delegation possible, safe, reproducible, and extremely easy to configure.**

---

## Key features

| Feature | Description |
|---|---|
| **Non-blocking delegation** | Submit a job and get a `job_id` immediately; poll when ready |
| **Background durability** | Jobs backed by Task Spooler (`ts`) survive MCP restarts and session closures |
| **Local GPU support** | Use Ollama or MLX models at zero API cost — today, not in a future release |
| **Git worktree isolation** | Each coding job runs in its own branch; you review and merge when satisfied |
| **Self-configuring** | Add agents, providers, and paths by talking to Claude — no config files, no restarts |
| **Parallel execution** | Multiple agents run concurrently — tell Claude "start phase 2 and parallelize whatever has no dependencies" and it figures out what can run in parallel and what has to wait |
| **Server-enforced guardrails** | Path allowlists, dangerous-command confirmation, shell-injection blocking, audit log — enforced by the server, *not* prompt instructions a worker could ignore. A worker physically cannot touch a path outside its allowlist, whatever the prompt says |
| **Agent clarification rounds** | Agents can ask the orchestrator questions before starting, preventing costly wrong assumptions |

---

## Supported agents

| Agent | Use case | Tested |
|---|---|---|
| **opencode** | Full-featured coding tasks, supports subscriptions | ✅ |
| **aider** | Git-aware code editing with SEARCH/REPLACE diffs | ✅ |
| **claude** (Claude Code CLI) | Delegate to Claude Code programmatically | ✅ |
| **codex** | OpenAI's Codex CLI | ✅ |
| **goose** | Block's agentic coding with model selection | ✅ |
| **hermes** | Multi-provider coding agent | ✅ |
| **jcode** | Jcode.ai agent built in Rust — fast tool-call execution | ✅ |
| **smolagents** | HuggingFace Python-based agentic execution | ✅ |
| **gptme** | Terminal-native agent with tool use | ✅ |
| **pi** | Pi coding agent | ✅ |
| **cline** | Autonomous coding agent with ACP support; configure any OpenAI-compatible provider via `cline auth` | ✅ |

> **Note on the `claude` worker:** Claude as a *worker* is best used pointed at other providers. Anthropic subscriptions cap usage per command, so a Claude-Code agent doing the heavy delegated work will hit those limits fast — the whole point of this tool is to keep your Claude *orchestrator* free. Use `claude` as a worker only for occasional high-quality tasks; send the bulk of delegated work to other providers (OpenRouter, local GPU, etc.).

Any CLI agent can be added without restarting the server. The server ships with a knowledge base of common agents; for anything new, just ask Claude to register it.

---

## Why unlimited-mcp?

| Without it | With it |
|---|---|
| Claude hits the 5h limit mid-task | Long tasks run in background agents, Claude stays free |
| You pay frontier prices for every line of code | Design with Opus, delegate execution to a cheaper/free LLM — you set the strategy |
| Closing Claude cancels in-progress work | Background jobs keep running after you close your session |
| Adding a new agent means editing config files | Tell Claude "add opencode with DeepSeek Flash" — done |
| Local GPU sits idle while paying for API | Ollama/MLX/LM Studio agents work out of the box |
| Infrastructure tasks need separate tooling | Run commands, scripts, and automation on any accessible server |

**vs. other MCP orchestrators**

| Feature | Typical MCP orchestrator | unlimited-mcp |
|---|---|---|
| Job durability | Session-scoped — lost on restart | Backed by Task Spooler, survives MCP restarts and session closures |
| Orchestrator ↔ agent communication | Fire and forget | `clarify_rounds` — agents ask questions before starting; orchestrator answers in real time |
| Worker isolation | Shared workspace | Git worktree per job — main branch never touched until you merge |
| Local/remote GPU | Rarely supported | Ollama, MLX, LM Studio — local or over SSH, zero API cost |

> **Not a synchronous Ollama bridge.** Tools like the local-LLM MCP servers hand one task to one local model and block until it answers. `unlimited-mcp` is production-grade async orchestration: durable background queues, multi-agent backends, git-worktree isolation, sysops/devops as well as coding, and SSH remote execution.

---

## Quick start

`unlimited-mcp` is available on PyPI — install it with pip or uv, then connect it to any MCP client (Claude Code, Cursor, Codex, Windsurf).

### 1. Install

```bash
# From PyPI (recommended)
pip install unlimited-mcp
# …or, isolated, with uv:
uv tool install unlimited-mcp        # then run:  uvx unlimited-mcp

# Or straight from GitHub (latest, unreleased)
pip install git+https://github.com/triumsebas/unlimited-mcp.git
```

**Prerequisites**: Python 3.11+. For background jobs: [`ts` (task-spooler)](https://viric.name/soft/ts/) on your PATH.

> **Get everything, not just the server.** Installing the package gives you the MCP server only — the opinionated `/delegate` skill and the orchestrator companion guides live in the repo. To pull them in, just ask your AI:
> - **Claude Code:** *"add the `/delegate` skill from `https://github.com/triumsebas/unlimited-mcp/tree/main/skills/delegate`"* (or, if you cloned the repo, give it the local path to `skills/delegate/`).
> - **Codex / other orchestrators:** *"add this project's AGENTS.md from `https://github.com/triumsebas/unlimited-mcp/blob/main/AGENTS.md` to my config"* (or, if you cloned the repo, give it the local path to `AGENTS.md`) so it has the full delegation reference.

### 2. Add to your MCP client

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_MCP-007ACC?logo=visualstudiocode)](vscode://mcp/install?name=unlimited-mcp&config=%7B%22command%22%3A%22unlimited-mcp%22%2C%22args%22%3A%5B%5D%7D)

Or add this snippet to your client's config file:

```json
{
  "mcpServers": {
    "unlimited-mcp": {
      "command": "unlimited-mcp",
      "args": []
    }
  }
}
```

<details>
<summary><b>Claude Code</b> — <code>~/.claude/settings.json</code></summary>

Add the `mcpServers` block above to your settings file, or ask Claude Code directly: *"add an MCP server called unlimited-mcp that runs the `unlimited-mcp` command"*.

</details>

<details>
<summary><b>Claude Desktop</b> — <code>claude_desktop_config.json</code></summary>

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`  
Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `mcpServers` block above and restart Claude Desktop.

</details>

<details>
<summary><b>Codex</b> — <code>~/.codex/config.json</code></summary>

Add the `mcpServers` block to your Codex config file, or ask Codex: *"add this MCP server to my config"* and paste the snippet.

</details>

<details>
<summary><b>Cursor / Windsurf / other MCP clients</b></summary>

Open the MCP settings panel in your client and add a new server with command `unlimited-mcp` and no arguments.

</details>

> **Tip:** You can skip all of this and just ask your AI: *"add an MCP server called unlimited-mcp that runs the `unlimited-mcp` command"* — it will edit the config file for you.

### 3. First session — just talk to Claude

No config files, no manual setup. Connect the MCP and start a conversation:

> **You:** "I just installed Opencode. Help me register it as a coding agent using DeepSeek Flash with OpenRouter."
>
> **Claude:** "I'll set that up. You'll need an API key for the provider — either tell me now or drop it in `~/.config/unlimited-mcp/.env` as `OPENROUTER_API_KEY` if you prefer to keep it out of the conversation."
>
> **You:** [key or: "done, I added it to .env"]
>
> **Claude:** "Registered `opencode_flash` — Opencode with DeepSeek Flash via OpenRouter. Which project do you want it to work on?"
>
> **You:** "/home/user/myproject"
>
> **Claude:** "Path allowed. Ready to go — what do you want Opencode to work on first?"

That's it. Every agent, provider, path, and safety rule is configured through conversation. Everything is persisted automatically. The creator of this project never manually edited a config file.

---

## Use cases

### AI-assisted software development

The pattern that motivated this project: an expensive model designs and
reviews, a cheap model implements for hours burning no subscription. You
decide which agent handles what and when the orchestrator steps back in —
the full Design → Implementation → Review flow is in
**[PROMPTS.md](PROMPTS.md)**.

Some operations you can offload:
- Writing boilerplate, tests, and docstrings
- Refactoring with well-defined acceptance criteria
- Fixing lint errors and type issues across large codebases
- Generating and running database migrations
- Reviewing patches and suggesting improvements

Because each task runs in an isolated git worktree, your main branch is never touched until you explicitly approve and merge.

---

### Systems operations

Automate infrastructure tasks that require many calls or long runtimes —
audit a fleet, roll out config changes, run long batch jobs in the
background. Runnable examples are in **[PROMPTS.md](PROMPTS.md)**.

Use cases:
- Audit many servers for compliance or security issues
- Roll out configuration changes across a fleet
- Run long batch jobs (data processing, ML training pipelines)
- Any automation that would block your terminal for minutes or hours

---

### Local GPU — zero API cost

If you have a GPU — local or on a machine at home or work — you can run entire coding sessions at zero API cost. Any agent that accepts an OpenAI-compatible endpoint works out of the box.

Supported local inference backends:

| Backend | Platform | Notes |
|---|---|---|
| **Ollama** | Mac · Linux · Windows | Easy setup, wide model library |
| **MLX** | Apple Silicon | Fast, native quantization |
| **LM Studio** | Mac · Windows | GUI + OpenAI-compat API server |

Just tell Claude which backend you're running and on which machine, and it will configure the agent against it. No code, no config files.

---

## Architecture

Component map, the full internal data flow, and the `JobResult` envelope are
in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Configuration

**You don't configure `unlimited-mcp` — you talk to your orchestrator and it configures itself.**

Every agent, provider, safety rule, and path permission is managed through conversation. No config files to edit, no syntax to learn, no server restarts.

Some examples of what you can say:

```
"Add opencode using DeepSeek Flash via OpenRouter for cheap tasks"
"Add a local agent using Ollama with qwen2.5-coder:32b"
"Add aider with the claude-sonnet-4-6 model for careful refactoring"
"Allow /home/user/projects so workers can access it"
"Show me what agents and providers are configured"
"Change opencode_flash to use deepseek-r1 instead"
"Remove the openrouter provider"
```

Everything persists automatically across sessions. API keys and secrets are stored securely, never alongside the configuration.

> **Note for developers:** configuration lives at `~/.config/unlimited-mcp/config.yaml` and can be inspected or backed up. But you'll rarely need to open it.

**Customising delegation routing:** the `/delegate` SKILL.md (Claude Code) and AGENTS.md (Codex) are starting points, not fixed rules. You can encode your own routing strategy directly in those files — for example: always send GPU-heavy tasks to a local GPU agent, use a fast cheap model for coding, reserve the strong model for architecture, route sysops work through smolagents with a fallback.

---

## Built with Claude · Tested with Claude and Codex

`unlimited-mcp` was designed and built using Claude as the primary orchestrator — including using the server itself to delegate implementation to cheaper models during development (see [the origin story](#the-problem-that-built-this)).

**Tested orchestrators:**
- [Claude Code](https://claude.ai/code) (Claude Code CLI and Desktop) — primary development environment
- [Codex](https://openai.com/codex) — tested as a drop-in orchestrator

Any MCP-compatible orchestrator should work with no or minor changes. If you test it with another one, [open an issue](https://github.com/triumsebas/unlimited-mcp/issues) — we'll add it to the list.

---

## Roadmap

### ✅ Phase 1 — Core orchestration
- MCP server with FastMCP
- Agent delegation (aider, opencode, claude, jcode...)
- Local async runner
- Git worktree workspace isolation
- Safety layer (path allowlists, argv blocking)
- Live configuration (no restart)

### ✅ Phase 2 — Background execution
- Task Spooler (`ts`) backend — durable jobs across MCP restarts
- Multi-queue routing (`local`, `ts`, `ts_serial`) with parallel execution
- `run_shell` with audit log
- Worker clarification rounds (`clarify_rounds`)
- Additional agent CLIs: goose, hermes, jcode, pi, gptme
- `query_logs` for operational observability
- Local inference: Ollama, MLX, LM Studio (any OpenAI-compatible endpoint)

### ✅ Phase 3 — Remote execution
- **SSH backend** — delegate tasks to remote machines; run commands, agents, and prompt-via-file over SSH.
  Authentication: macOS Keychain (tested ✓) · ssh-agent (tested ✓) · Linux keyring (pending).
  In **Claude Desktop**, the server has no SSH agent socket — use Keychain. In **Claude Code (terminal)**, both ssh-agent and Keychain work.
  See [SSH.md](SSH.md) for setup.
- **Remote GPU clusters** — submit jobs to remote Ollama/MLX/LM Studio servers

### 🔜 Phase 4 — Notifications & observability
- **ACP client backend** *(first priority)* — native [Agent Client Protocol](https://agentclientprotocol.com/) support so any ACP-compatible agent works as a worker without a custom adapter. The [ACP registry](https://github.com/agentclientprotocol/registry) lists 30+ agents already shipping ACP — OpenCode (`opencode acp`), Cline, Gemini CLI, Goose, Kilo, Dirac, and more. Benefits over the current CLI model: streaming progress replaces polling, structured diffs replace raw patch files, and `session/set_mode` maps cleanly to workspace presets.
- **Anthropic direct provider** — planned if there is enough demand. In the meantime, LiteLLM (local proxy) and OpenRouter (cloud) already cover this with no additional setup: both expose an OpenAI-compatible endpoint that works with the existing provider configuration.
- **Webhook / instant messaging notifications** — get notified when long background jobs complete
- **Web dashboard** — monitor active and completed jobs visually

---

## Security

`unlimited-mcp` runs commands with **your** privileges over a local stdio
transport — there is no network listener, but the safety layer is a best-effort
guardrail, **not an OS-level sandbox**. Read the
[**Security / Threat model**](SECURITY.md) before pointing it at anything you
care about.

---

## Documentation

| File | What it's for |
|---|---|
| [README.md](README.md) | This file — overview, quick start, use cases, roadmap |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal component map, data flow, and the `JobResult` envelope |
| [PROMPTS.md](PROMPTS.md) | Copy-paste example prompts and delegation flows (dev, sysops) |
| [SECURITY.md](SECURITY.md) | Threat model — access model, privilege level, what the safety layer does and does not protect |
| [AGENTS.md](AGENTS.md) | **Canonical orchestration reference** — decision trees, queues, timeouts, safety, `clarify_rounds`, agent config. Read natively by Codex; imported by CLAUDE.md |
| [CLAUDE.md](CLAUDE.md) | Claude Code entry point — imports AGENTS.md and adds only the Claude-Code-specific notes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Developer guide — how to add agents, run tests, contribute code |
| [skills/delegate/SKILL.md](skills/delegate/SKILL.md) | The `/delegate` skill for Claude Code — opinionated delegation patterns; defers to AGENTS.md for the tool reference |
| [skills/selftest/SKILL.md](skills/selftest/SKILL.md) | The `/selftest` skill for Claude Code — runs the Tier A + Tier B regression battery and reports pass/fail |
| [knowledge.yaml](src/unlimited_mcp/knowledge.yaml) | Built-in catalog of agent CLIs and providers — what the server knows before you configure anything |

---

## Contributing

`unlimited-mcp` is early-stage and contributions are very welcome — especially:

- **Large project users** — if you're working on a big codebase or running heavy automation, your feedback is the most valuable thing right now. Real-world usage exposes what toy examples don't.
- **New agent CLIs** — if you use a coding agent not in the knowledge base, add it
- **SSH backend** — keyring + agent auth shipped and tested on macOS; Linux keyring and Windows are still needed
- **Tests** — the test suite grows with every feature, more coverage always helps
- **Documentation** — walkthroughs, examples, and troubleshooting guides

See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

---

## About

Built by **Sebastián Fernández** · CEO at [Trium Sistemas Informáticos SL](https://www.triumsistemas.com)

A systems engineer who dove deep into AI tooling — and built the infrastructure he wished existed.

[![GitHub](https://img.shields.io/badge/GitHub-triumsebas-181717?logo=github)](https://github.com/triumsebas)

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

<!-- mcp-name: io.github.triumsebas/unlimited-mcp -->
