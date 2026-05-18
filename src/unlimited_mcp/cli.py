# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Console entry point for ``unlimited-mcp``.

Subcommands:
  init    Create default config, .env, and knowledge.local.yaml.
  serve   Start the MCP server (stdio transport).
  doctor  Check environment: config validity, binaries, recent failures.
  jobs    Job sub-commands (ls).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage: unlimited-mcp <subcommand> [options]\n"
            "\n"
            "Subcommands:\n"
            "  init    Create default config files and print MCP snippet.\n"
            "  serve   Start the MCP server over stdio (default transport).\n"
            "  doctor  Check environment: config, binaries, recent failures.\n"
            "  jobs    Manage background jobs (jobs ls).\n"
            "\n"
            "Options:\n"
            "  -h, --help   Show this message.",
            file=sys.stderr,
        )
        return 0 if args else 2

    subcmd = args[0]
    if subcmd == "serve":
        return _cmd_serve(args[1:])
    if subcmd == "init":
        return _cmd_init(args[1:])
    if subcmd == "doctor":
        return _cmd_doctor(args[1:])
    if subcmd == "jobs":
        return _cmd_jobs(args[1:])

    print(
        f"unlimited-mcp: unknown subcommand {subcmd!r}. Run with --help for usage.",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# dotenv loader
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load .env files into os.environ (does not overwrite existing vars).

    Checks in order:
      1. ~/.config/unlimited-mcp/.env  (user config dir, production)
      2. The repo root .env next to the installed package (dev convenience)
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    from unlimited_mcp.paths import config_dir

    candidates = [
        config_dir() / ".env",
        Path(__file__).parent.parent.parent / ".env",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _cmd_serve(args: list[str]) -> int:
    from unlimited_mcp.paths import ensure_dirs
    from unlimited_mcp.server import make_server

    if "-h" in args or "--help" in args:
        print(
            "unlimited-mcp serve\n"
            "\n"
            "Start the MCP server over stdio. All configuration is read from\n"
            "~/.config/unlimited-mcp/config.yaml on startup.",
            file=sys.stderr,
        )
        return 0

    ensure_dirs()
    _load_dotenv()
    app = make_server()
    app.run()
    return 0


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = """\
schema_version: 1

# Safety: workers can only access paths listed here.
# Add paths with: unlimited-mcp add_allowed_root <path>
# or via MCP tool: add_allowed_root(path)
allowed_roots:
  - /tmp/unlimited-mcp

deny_paths: []

safety:
  allow_shell_like_argv: false
  default_safety_policy: standard
  confirm_token_ttl_seconds: 300

# Providers — uncomment and fill to use run_and_summarize summarisation.
# providers:
#   opencode_default:
#     type: openai_compat
#     model: deepseek-v3
#     base_url: https://opencode.ai/zen/go/v1
#     api_key_env: OPENCODE_API_KEY

# Agents — register your local workers here.
# agents:
#   aider_local:
#     cli: aider
#     workspace: safe_dev
#     params:
#       git: true
#       auto_commits: false
"""

_DEFAULT_ENV = """\
# unlimited-mcp secrets — gitignored
# Add your API keys here, one per line:
# OPENCODE_API_KEY=your-key-here
# OPENAI_API_KEY=your-key-here
"""


def _cmd_init(args: list[str]) -> int:
    from unlimited_mcp.paths import config_dir, config_path, ensure_dirs, knowledge_local_path

    if "-h" in args or "--help" in args:
        print(
            "unlimited-mcp init\n"
            "\n"
            "Create default config files under ~/.config/unlimited-mcp/ and\n"
            "print the MCP snippet to add to your Claude Code config.",
            file=sys.stderr,
        )
        return 0

    ensure_dirs()
    Path("/tmp/unlimited-mcp").mkdir(parents=True, exist_ok=True)

    cfg = config_path()
    env = config_dir() / ".env"
    kl = knowledge_local_path()
    created = []

    if not cfg.exists():
        cfg.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        created.append(str(cfg))
    else:
        print(f"  config.yaml already exists: {cfg}", file=sys.stderr)

    if not env.exists():
        env.write_text(_DEFAULT_ENV, encoding="utf-8")
        created.append(str(env))

    if not kl.exists():
        kl.write_text("# Local knowledge overrides (gitignored)\n", encoding="utf-8")
        created.append(str(kl))

    if created:
        print("Created:", file=sys.stderr)
        for f in created:
            print(f"  {f}", file=sys.stderr)

    server_path = shutil.which("unlimited-mcp") or sys.executable + " -m unlimited_mcp"
    print("\n--- Add to your Claude Code MCP config (claude_desktop_config.json) ---")
    print(json.dumps(
        {
            "mcpServers": {
                "unlimited-mcp": {
                    "command": server_path,
                    "args": ["serve"],
                }
            }
        },
        indent=2,
    ))
    print("\nThen restart Claude Code and run: list_capabilities()")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _cmd_doctor(args: list[str]) -> int:
    import yaml

    from unlimited_mcp.paths import audit_dir, config_path, knowledge_local_path, state_dir

    if "-h" in args or "--help" in args:
        print("unlimited-mcp doctor\n\nCheck config validity, binary availability, and recent errors.")
        return 0

    ok = True

    # 1. config.yaml
    cfg_file = config_path()
    if not cfg_file.exists():
        print(f"[WARN] config.yaml missing: {cfg_file}")
        print("       Run: unlimited-mcp init")
        ok = False
    else:
        try:
            from unlimited_mcp.config.loader import ConfigStore
            from unlimited_mcp.config.schema import Config

            cfg = ConfigStore(cfg_file).get()
            print(f"[OK]  config.yaml valid ({cfg_file})")
            if not cfg.allowed_roots:
                print("[WARN]   allowed_roots is empty — no commands will be permitted.")
                ok = False
            else:
                print(f"[OK]    allowed_roots: {cfg.allowed_roots}")
        except Exception as exc:
            print(f"[FAIL] config.yaml invalid: {exc}")
            ok = False

    # 2. knowledge.local.yaml
    kl = knowledge_local_path()
    print(f"[{'OK' if kl.exists() else 'INFO'}]  knowledge.local.yaml: {kl}")

    # 3. Binaries
    for binary in ("aider", "opencode", "uv", "git"):
        path = shutil.which(binary)
        status = "[OK] " if path else "[INFO]"
        detail = path or "not found on PATH"
        print(f"{status} {binary}: {detail}")

    # 4. Runtime state dir
    state = state_dir()
    print(f"[OK]  state dir: {state}")

    # 5. Recent failures
    errors_file = audit_dir() / "errors.jsonl"
    if errors_file.exists():
        lines = errors_file.read_text(encoding="utf-8").splitlines()
        recent = lines[-5:] if lines else []
        if recent:
            print(f"\nLast {len(recent)} error(s) from audit/errors.jsonl:")
            for line in recent:
                try:
                    entry = json.loads(line)
                    print(f"  {entry.get('ts', '?')} {entry.get('event', line[:80])}")
                except json.JSONDecodeError:
                    print(f"  {line[:80]}")
    else:
        print("[OK]  No errors.jsonl found (clean slate).")

    return 0 if ok else 1


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def _cmd_jobs(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage: unlimited-mcp jobs <subcommand>\n"
            "\n"
            "Subcommands:\n"
            "  ls   List all known jobs.",
            file=sys.stderr,
        )
        return 0 if args else 2

    if args[0] == "ls":
        return _cmd_jobs_ls(args[1:])

    print(f"unlimited-mcp jobs: unknown subcommand {args[0]!r}.", file=sys.stderr)
    return 2


def _cmd_jobs_ls(args: list[str]) -> int:
    from unlimited_mcp.jobs.store import JobStore
    from unlimited_mcp.paths import jobs_dir

    store = JobStore(jobs_dir())
    job_ids = store.list_jobs()
    if not job_ids:
        print("No jobs found.")
        return 0

    print(f"{'JOB ID':<36}  {'STATUS':<20}  {'TOOL':<22}  SUMMARY")
    print("-" * 100)
    for job_id in sorted(job_ids):
        result = store.read_result(job_id)
        if result is None:
            print(f"{job_id:<36}  {'(no result)':<20}  {'?':<22}")
            continue
        summary = (result.summary or "")[:60]
        print(f"{job_id:<36}  {result.status:<20}  {result.tool:<22}  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
