"""Console entry point for ``unlimited-mcp``.

Real subcommands (``init``, ``serve``, ``doctor``, ``jobs``) land in phase 1.
Phase 0 only registers the entry point so packaging and ``uv tool install``
flows can be exercised end-to-end.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in {"-h", "--help"}:
        print(
            "unlimited-mcp: orchestration MCP server (phase 0).\n"
            "Subcommands `init`, `serve`, `doctor`, `jobs` will be implemented "
            "in phase 1.",
            file=sys.stderr,
        )
        return 0 if args else 2
    print(
        f"unlimited-mcp: subcommand {args[0]!r} is not implemented yet.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
