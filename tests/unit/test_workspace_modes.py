"""Unit tests for :mod:`unlimited_mcp.workspace.modes`."""

from __future__ import annotations

import pytest

from unlimited_mcp.config.schema import (
    Knowledge,
    WorkspacePreset,
    WorkspaceSpec,
)
from unlimited_mcp.workspace.modes import builtin_preset_names, resolve_workspace


def test_none_resolves_to_sysops_local() -> None:
    spec = resolve_workspace(None, Knowledge())
    assert spec.mode == "none"
    assert spec.result == "report_only"


def test_explicit_workspace_spec_passed_through() -> None:
    explicit = WorkspaceSpec(mode="git_worktree", result="leave_branch", dirty="refuse")
    assert resolve_workspace(explicit, Knowledge()) is explicit


def test_builtin_safe_dev() -> None:
    spec = resolve_workspace("safe_dev", Knowledge())
    assert spec.mode == "git_worktree"
    assert spec.result == "leave_branch"
    assert spec.dirty == "refuse"


def test_all_builtins_resolve() -> None:
    """Every name reported by :func:`builtin_preset_names` must resolve."""
    for name in builtin_preset_names():
        spec = resolve_workspace(name, Knowledge())
        assert isinstance(spec, WorkspaceSpec)


def test_knowledge_preset_overrides_builtin() -> None:
    """A preset declared in Knowledge wins over the same-named builtin."""
    k = Knowledge(
        workspace_presets={
            "safe_dev": WorkspacePreset(mode="temp_copy", result="report_only", dirty=None)
        }
    )
    spec = resolve_workspace("safe_dev", k)
    assert spec.mode == "temp_copy"
    assert spec.result == "report_only"


def test_unknown_preset_raises() -> None:
    with pytest.raises(KeyError, match="Unknown workspace preset"):
        resolve_workspace("totally_made_up", Knowledge())


def test_each_resolution_returns_fresh_instance() -> None:
    """Resolutions don't share mutable state across callers."""
    a = resolve_workspace("read_only", Knowledge())
    b = resolve_workspace("read_only", Knowledge())
    assert a == b
    assert a is not b
