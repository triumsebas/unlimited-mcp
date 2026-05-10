"""Unit tests for :func:`unlimited_mcp.safety.classes.classify_argv`."""

from __future__ import annotations

from unlimited_mcp.config.schema import (
    FlagPattern,
    Knowledge,
    ToolKnowledge,
)
from unlimited_mcp.safety.classes import classify_argv


def _knowledge(**tools: ToolKnowledge) -> Knowledge:
    return Knowledge(tools=dict(tools))


def test_empty_argv_is_unknown() -> None:
    assert classify_argv([], Knowledge()) == "unknown"


def test_unknown_cli_is_unknown() -> None:
    assert classify_argv(["mystery"], Knowledge()) == "unknown"


def test_known_cli_returns_base_class() -> None:
    k = _knowledge(rg=ToolKnowledge(safety_class="read"))
    assert classify_argv(["rg", "pattern"], k) == "read"


def test_basename_resolves_path_to_command() -> None:
    """Argv[0] may be /usr/local/bin/rg — we still match by basename."""
    k = _knowledge(rg=ToolKnowledge(safety_class="read"))
    assert classify_argv(["/usr/local/bin/rg", "pattern"], k) == "read"


def test_escalation_flag_pattern() -> None:
    k = _knowledge(
        rm=ToolKnowledge(
            safety_class="mutating",
            flag_patterns=[
                FlagPattern(match=["-rf", "-r", "-fr"], escalates_to="dangerous"),
            ],
        )
    )
    assert classify_argv(["rm", "-rf", "/tmp/foo"], k) == "dangerous"
    assert classify_argv(["rm", "/tmp/foo"], k) == "mutating"  # plain rm stays mutating


def test_demotion_flag_pattern() -> None:
    k = _knowledge(
        kubectl=ToolKnowledge(
            safety_class="mutating",
            flag_patterns=[
                FlagPattern(match=["get", "describe"], demotes_to="read"),
            ],
        )
    )
    assert classify_argv(["kubectl", "get", "pods"], k) == "read"


def test_escalation_wins_over_demotion() -> None:
    """If the user runs `kubectl delete -o yaml`, the demotion via `get` would
    not even apply, but if both escalator and demotion match, escalation wins."""
    k = _knowledge(
        kubectl=ToolKnowledge(
            safety_class="mutating",
            flag_patterns=[
                FlagPattern(match=["delete", "apply"], escalates_to="dangerous"),
                FlagPattern(match=["get", "describe"], demotes_to="read"),
            ],
        )
    )
    assert classify_argv(["kubectl", "delete", "pod", "x"], k) == "dangerous"
    # Both keywords present → escalation still wins.
    assert classify_argv(["kubectl", "delete", "get", "x"], k) == "dangerous"


def test_no_match_keeps_base_class() -> None:
    k = _knowledge(
        rm=ToolKnowledge(
            safety_class="mutating",
            flag_patterns=[FlagPattern(match=["-rf"], escalates_to="dangerous")],
        )
    )
    assert classify_argv(["rm", "foo.txt"], k) == "mutating"


def test_dangerous_base_class_remains_dangerous() -> None:
    k = _knowledge(scp=ToolKnowledge(safety_class="dangerous"))
    assert classify_argv(["scp", "a", "host:b"], k) == "dangerous"
