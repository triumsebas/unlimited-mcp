"""Console entry point for ``unlimited-mcp``.

Subcommands:
  serve   Start the MCP server (stdio transport, default).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage: unlimited-mcp <subcommand> [options]\n"
            "\n"
            "Subcommands:\n"
            "  serve   Start the MCP server over stdio (default transport).\n"
            "\n"
            "Options:\n"
            "  -h, --help   Show this message.",
            file=sys.stderr,
        )
        return 0 if args else 2

    if args[0] == "serve":
        return _cmd_serve(args[1:])

    print(
        f"unlimited-mcp: unknown subcommand {args[0]!r}. Run with --help for usage.",
        file=sys.stderr,
    )
    return 2


def _cmd_serve(args: list[str]) -> int:
    from unlimited_mcp.paths import ensure_dirs
    from unlimited_mcp.server import make_server

    if "-h" in args or "--help" in args:
        print(
            "unlimited-mcp serve\n"
            "\n"
            "Start the MCP server over stdio.  All configuration is read from\n"
            "~/.config/unlimited-mcp/config.yaml on startup.",
            file=sys.stderr,
        )
        return 0

    ensure_dirs()
    app = make_server()
    app.run()  # blocks until EOF on stdin
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
