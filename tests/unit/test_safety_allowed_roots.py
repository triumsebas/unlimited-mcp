"""Unit tests for :mod:`unlimited_mcp.safety.allowed_roots`."""

from __future__ import annotations

from pathlib import Path

from unlimited_mcp.config.schema import PathFlag, ToolKnowledge
from unlimited_mcp.safety.allowed_roots import (
    check_paths,
    find_path_args,
    is_within_allowed_roots,
)

# ---------------- find_path_args -------------------------------------


def test_bare_positional_paths_extracted() -> None:
    paths = find_path_args(["ls", "/etc", "./local", "../up", "~/home"], None)
    assert paths == ["/etc", "./local", "../up", "~/home"]


def test_non_path_positionals_ignored() -> None:
    paths = find_path_args(["rg", "pattern", "--threads=4"], None)
    assert paths == []


def test_stdin_dash_ignored() -> None:
    paths = find_path_args(["cat", "-"], None)
    assert paths == []


def test_path_flag_separate_style() -> None:
    tool = ToolKnowledge(
        safety_class="mutating",
        path_flags=[PathFlag(flag="-C", style="separate")],
    )
    paths = find_path_args(["tar", "-C", "/etc", "-x"], tool)
    assert "/etc" in paths


def test_path_flag_combined_style() -> None:
    tool = ToolKnowledge(
        safety_class="mutating",
        path_flags=[PathFlag(flag="--config", style="combined")],
    )
    paths = find_path_args(["app", "--config=/etc/app.conf"], tool)
    assert paths == ["/etc/app.conf"]


def test_path_flag_separate_or_equals_accepts_both_forms() -> None:
    tool = ToolKnowledge(
        safety_class="mutating",
        path_flags=[PathFlag(flag="-f", style="separate_or_equals")],
    )
    paths_a = find_path_args(["kubectl", "apply", "-f", "/etc/manifest.yaml"], tool)
    paths_b = find_path_args(["kubectl", "apply", "-f=/etc/manifest.yaml"], tool)
    assert "/etc/manifest.yaml" in paths_a
    assert "/etc/manifest.yaml" in paths_b


def test_path_flag_value_dash_is_stdin_ignored() -> None:
    tool = ToolKnowledge(
        safety_class="mutating",
        path_flags=[PathFlag(flag="-f")],
    )
    assert find_path_args(["kubectl", "apply", "-f", "-"], tool) == []


def test_empty_argv_returns_empty() -> None:
    assert find_path_args([], None) == []


# ---------------- is_within_allowed_roots ----------------------------


def test_empty_allowed_roots_denies_everything() -> None:
    assert is_within_allowed_roots("/tmp/foo", [], []) is False


def test_path_inside_allowed_root(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert is_within_allowed_roots(str(tmp_path / "child"), [root], []) is True


def test_path_outside_allowed_root(tmp_path: Path) -> None:
    root = str(tmp_path / "scope")
    assert is_within_allowed_roots(str(tmp_path / "outside"), [root], []) is False


def test_deny_path_overrides_allowed_root(tmp_path: Path) -> None:
    root = str(tmp_path)
    deny = str(tmp_path / "secrets")
    assert is_within_allowed_roots(str(tmp_path / "secrets" / "key"), [root], [deny]) is False


def test_tilde_expansion(tmp_path: Path, monkeypatch) -> None:
    """`~/foo` resolves through HOME expansion."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert is_within_allowed_roots("~/code/proj", [str(tmp_path)], []) is True


def test_path_does_not_need_to_exist(tmp_path: Path) -> None:
    """Containment check works on non-existent paths so we can validate
    write targets before creating them."""
    root = str(tmp_path)
    assert is_within_allowed_roots(str(tmp_path / "future" / "file.txt"), [root], []) is True


# ---------------- check_paths ----------------------------------------


def test_check_paths_returns_first_offender(tmp_path: Path) -> None:
    root = str(tmp_path)
    paths = [str(tmp_path / "ok"), "/etc/foo", str(tmp_path / "also_ok")]
    assert check_paths(paths, [root], []) == "/etc/foo"


def test_check_paths_returns_none_when_all_allowed(tmp_path: Path) -> None:
    root = str(tmp_path)
    paths = [str(tmp_path / "a"), str(tmp_path / "b")]
    assert check_paths(paths, [root], []) is None
