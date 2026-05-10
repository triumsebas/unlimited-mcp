"""Resolve workspace presets to fully-explicit :class:`WorkspaceSpec`.

The orchestrator passes either a preset name (``"safe_dev"``,
``"read_only"`` ...), an explicit :class:`WorkspaceSpec`, or ``None``.
Resolution looks first in :attr:`Knowledge.workspace_presets` (the user
or repo can override builtins), then falls back to the built-ins below.
Unknown names raise ``KeyError`` so misspellings fail loudly.
"""

from __future__ import annotations

from typing import Any

from unlimited_mcp.config.schema import Knowledge, WorkspaceSpec

#: Built-in presets, identical to plan §5. The data lives as plain dicts so
#: every call constructs a fresh validated WorkspaceSpec instance — no
#: shared mutable state across resolutions.
_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "safe_dev": {
        "mode": "git_worktree",
        "result": "leave_branch",
        "dirty": "refuse",
    },
    "quick_edit": {
        "mode": "current",
        "result": "apply_direct",
        "dirty": "refuse",
    },
    "read_only": {
        "mode": "current",
        "result": "report_only",
        "dirty": "allow",
    },
    "sysops_local": {
        "mode": "none",
        "result": "report_only",
        "dirty": None,
    },
    "sysops_remote": {
        "mode": "remote_cwd",
        "result": "report_only",
        "dirty": None,
    },
    "destructive_apply": {
        "mode": "none",
        "result": "apply_direct",
        "dirty": None,
    },
}


def builtin_preset_names() -> tuple[str, ...]:
    """Return the names of all built-in presets, for ``list_capabilities``."""
    return tuple(_BUILTIN_PRESETS)


def resolve_workspace(
    spec: str | WorkspaceSpec | None,
    knowledge: Knowledge,
) -> WorkspaceSpec:
    """Return a fully-explicit :class:`WorkspaceSpec`.

    Resolution order:

    1. ``None`` → ``sysops_local`` (no workspace, report only).
    2. Already a :class:`WorkspaceSpec` → returned unchanged.
    3. preset name → ``Knowledge.workspace_presets[name]`` if present,
       else the built-in with the same name.
    4. Unknown name → ``KeyError``.
    """
    if spec is None:
        return WorkspaceSpec(**_BUILTIN_PRESETS["sysops_local"])
    if isinstance(spec, WorkspaceSpec):
        return spec
    preset = knowledge.workspace_presets.get(spec)
    if preset is not None:
        return WorkspaceSpec(
            mode=preset.mode,
            result=preset.result,
            dirty=preset.dirty,
        )
    builtin = _BUILTIN_PRESETS.get(spec)
    if builtin is None:
        raise KeyError(f"Unknown workspace preset: {spec!r}")
    return WorkspaceSpec(**builtin)
