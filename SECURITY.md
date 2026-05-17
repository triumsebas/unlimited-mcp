# Security / Threat model

`unlimited-mcp` is a **power tool**. It runs commands and delegates work to local
agents. Read this before exposing it to anything you care about.

## How it's accessed

The server speaks **stdio only**. It does not open a port, socket, or network
listener. It is started as a subprocess by the MCP client you configure (e.g.
Claude Code) and communicates over stdin/stdout. There is no remote attack
surface and no authentication layer because there is nothing on the network to
authenticate — only a local process you launched can talk to it.

## Privilege level — read this

Workers run with the **full privileges of the user who started the MCP client**.
The OS does not confine them. The orchestrating model can, in effect, run
arbitrary local commands as you. Only point this at repos and machines where
that is acceptable.

## What the safety layer is — and is not

The `allowed_roots` / argv-classification layer is a **best-effort guardrail
against accidental footguns, not an OS-level sandbox**:

- Path checks only inspect *detected* path arguments (declared `path_flags` plus
  bare `/`, `./`, `../`, `~/` tokens). A program can still read or write
  anywhere at runtime — e.g. `python script.py`, `make`, `npm run`, or any
  interpreter — regardless of `allowed_roots`.
- CLIs not classified as `dangerous` in `knowledge.yaml` run **without
  confirmation**. Only explicitly-dangerous commands force a confirmation token.
- Shell-like inline code (`bash -lc`, `python -c`, …) is **blocked by default**
  (`safety.allow_shell_like_argv: false`). Keep it disabled unless you fully
  trust the orchestrator.

Treat `allowed_roots` as "where the tool will *try* to keep things", not as a
security boundary you can rely on against a malicious or jailbroken orchestrator.

## SSH agent forwarding

`forward_agent` (per-host, opt-in) forwards your local SSH agent to the remote
host so workers can authenticate to e.g. GitHub without storing credentials
remotely. If you forward your agent to a host you do not fully trust, that host
can use your loaded keys for the duration of the session. Only enable it for
hosts you control. Unknown hosts are rejected (`RejectPolicy`); accept the
fingerprint once with `ssh user@host` before configuring them.

## Secrets

Secrets live in `~/.config/unlimited-mcp/.env` (or the repo `.env` as a dev
fallback). Both are gitignored. Never commit API keys, private keys, or
`config.yaml` with embedded credentials.

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/triumsebas/unlimited-mcp/security/advisories/new)
rather than a public issue.
