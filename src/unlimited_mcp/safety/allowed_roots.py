# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Allowed-roots and deny-paths checks for argv invocations.

Path detection has three layers, in order of precision:

1. **Declared ``path_flags``** in :class:`Knowledge.tools` — the only
   source of truth for "this CLI's ``-f`` takes a file path". Supports
   three styles: ``combined`` (``--config=/etc/foo``), ``separate``
   (``-C /etc``), and ``separate_or_equals`` (either form).
2. **Bare positional paths** — anything starting with ``/``, ``./``,
   ``../``, or ``~/``.
3. *(Fallback intended for phase 2)* heuristic match of any token
   containing ``/`` that resolves to a real on-disk path. Not enabled
   yet — it generates noise without strong knowledge.yaml backing.

Limitation: remote-side paths in ``scp``/``rsync`` (``host:/etc/foo``)
are not parsed. Those CLIs are class ``dangerous`` by default in
``knowledge.yaml`` so confirmation is forced regardless.
"""

from __future__ import annotations

from pathlib import Path

from unlimited_mcp.config.schema import ToolKnowledge

_BARE_PATH_PREFIXES: tuple[str, ...] = ("/", "./", "../", "~/")
_STDIN_LITERALS: frozenset[str] = frozenset({"-", ""})


def find_path_args(argv: list[str], tool: ToolKnowledge | None) -> list[str]:
    """Return path-shaped argv elements (best-effort).

    Order is preserved and duplicates are kept — callers may want to
    associate each path with the flag that introduced it for auditing.
    """
    if not argv:
        return []
    paths: list[str] = []

    if tool is not None:
        rest = argv[1:]
        for pf in tool.path_flags:
            for i, arg in enumerate(rest):
                # combined / equals form: --flag=value
                if pf.style in ("combined", "separate_or_equals") and arg.startswith(f"{pf.flag}="):
                    value = arg.split("=", 1)[1]
                    if value not in _STDIN_LITERALS:
                        paths.append(value)
                # separate form: --flag value (next argv slot)
                if (
                    pf.style in ("separate", "separate_or_equals")
                    and arg == pf.flag
                    and i + 1 < len(rest)
                ):
                    value = rest[i + 1]
                    if value not in _STDIN_LITERALS:
                        paths.append(value)

    # bare positional paths (skip argv[0], which is the binary)
    for arg in argv[1:]:
        if arg in _STDIN_LITERALS:
            continue
        if arg.startswith(_BARE_PATH_PREFIXES):
            paths.append(arg)

    return paths


def _expand(p: str) -> Path:
    """Expand ``~`` and resolve to absolute (without requiring existence)."""
    return Path(p).expanduser().resolve(strict=False)


def is_within_allowed_roots(
    path: str,
    allowed_roots: list[str],
    deny_paths: list[str],
) -> bool:
    """``True`` iff ``path`` is inside some allowed root *and* outside
    every deny path. Empty ``allowed_roots`` always returns ``False`` —
    deny-by-default is the public-repo posture."""
    if not allowed_roots:
        return False
    target = _expand(path)

    for deny in deny_paths:
        if target.is_relative_to(_expand(deny)):
            return False

    return any(target.is_relative_to(_expand(root)) for root in allowed_roots)


def check_paths(
    paths: list[str],
    allowed_roots: list[str],
    deny_paths: list[str],
) -> str | None:
    """Return the first offending path, or ``None`` if all are allowed.

    Convenience for the safety pipeline so it can return the specific
    path that triggered ``OUT_OF_ROOT`` in its hint.
    """
    for p in paths:
        if not is_within_allowed_roots(p, allowed_roots, deny_paths):
            return p
    return None
