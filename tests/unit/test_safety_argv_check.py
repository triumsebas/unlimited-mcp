"""Unit tests for :class:`unlimited_mcp.safety.argv_check.SafetyChecker`.

Covers the integrated pipeline: classification + shell-like detection
+ allowed-roots + confirmation tokens.
"""

from __future__ import annotations

from pathlib import Path

from unlimited_mcp.config.schema import (
    Config,
    FlagPattern,
    Knowledge,
    PathFlag,
    SafetyConfig,
    ShellLikeArgvSpec,
    ToolKnowledge,
)
from unlimited_mcp.safety.argv_check import SafetyChecker


def _make_checker(
    *,
    allowed_roots: list[str] | None = None,
    deny_paths: list[str] | None = None,
    allow_shell_like_argv: bool = False,
    tools: dict[str, ToolKnowledge] | None = None,
    shell_like: dict[str, ShellLikeArgvSpec] | None = None,
) -> SafetyChecker:
    config = Config(
        safety=SafetyConfig(allow_shell_like_argv=allow_shell_like_argv),
        allowed_roots=allowed_roots or [],
        deny_paths=deny_paths or [],
    )
    knowledge = Knowledge(
        tools=tools or {},
        shell_like_argv=shell_like or {},
    )
    return SafetyChecker(config, knowledge)


def test_empty_argv_refused() -> None:
    decision = _make_checker().check_run_command([])
    assert decision.allowed is False
    assert decision.error_code == "SHELL_LIKE_BLOCKED"


def test_read_command_inside_root_allowed(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={"rg": ToolKnowledge(safety_class="read")},
    )
    decision = checker.check_run_command(["rg", "pattern", str(tmp_path / "file")])
    assert decision.allowed is True
    assert decision.safety_class == "read"
    assert decision.risk_level == "low"


def test_path_outside_root_refused(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={"rg": ToolKnowledge(safety_class="read")},
    )
    decision = checker.check_run_command(["rg", "pattern", "/etc/passwd"])
    assert decision.allowed is False
    assert decision.error_code == "OUT_OF_ROOT"
    assert "/etc/passwd" in (decision.error_hint or "")
    assert "add_allowed_root" in (decision.error_hint or "")


def test_cwd_outside_root_refused(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={"ls": ToolKnowledge(safety_class="read")},
    )
    decision = checker.check_run_command(["ls"], cwd="/etc")
    assert decision.allowed is False
    assert decision.error_code == "OUT_OF_ROOT"


def test_deny_path_blocks_even_inside_root(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        deny_paths=[str(secrets_dir)],
        tools={"cat": ToolKnowledge(safety_class="read")},
    )
    decision = checker.check_run_command(["cat", str(secrets_dir / "key.pem")])
    assert decision.allowed is False
    assert decision.error_code == "OUT_OF_ROOT"


def test_path_flag_value_is_checked(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={
            "kubectl": ToolKnowledge(
                safety_class="mutating",
                path_flags=[PathFlag(flag="-f", style="separate_or_equals")],
            )
        },
    )
    decision = checker.check_run_command(["kubectl", "apply", "-f", "/etc/manifest.yaml"])
    assert decision.allowed is False
    assert decision.error_code == "OUT_OF_ROOT"


def test_shell_like_blocked_by_default(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        shell_like={"bash": ShellLikeArgvSpec(inline_flags=["-c", "-lc"])},
    )
    decision = checker.check_run_command(["bash", "-lc", "echo hi"], cwd=str(tmp_path))
    assert decision.allowed is False
    assert decision.error_code == "SHELL_LIKE_BLOCKED"
    assert decision.safety_class == "dangerous"


def test_shell_like_allowed_with_flag_requires_confirmation(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        allow_shell_like_argv=True,
        shell_like={"bash": ShellLikeArgvSpec(inline_flags=["-c", "-lc"])},
    )
    decision = checker.check_run_command(["bash", "-lc", "echo hi"], cwd=str(tmp_path))
    # First call: allowed=False, but with a confirm_token the orchestrator
    # surfaces to the user.
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert decision.confirm_token is not None
    assert decision.safety_class == "dangerous"


def test_shell_like_does_not_match_unrelated_flags(tmp_path: Path) -> None:
    """``bash some-script.sh`` is shell-like as a binary but has no inline
    ``-c``/``-lc`` flag, so it must NOT trigger the gate."""
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        shell_like={"bash": ShellLikeArgvSpec(inline_flags=["-c", "-lc"])},
        tools={},
    )
    decision = checker.check_run_command(["bash", str(tmp_path / "script.sh")], cwd=str(tmp_path))
    assert decision.allowed is True


def test_dangerous_command_requires_confirmation(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={
            "rm": ToolKnowledge(
                safety_class="mutating",
                flag_patterns=[FlagPattern(match=["-rf"], escalates_to="dangerous")],
            )
        },
    )
    target = tmp_path / "victim"
    decision = checker.check_run_command(["rm", "-rf", str(target)])
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert decision.confirm_token is not None
    assert decision.confirm_reason and "dangerous" in decision.confirm_reason
    assert decision.risk_level == "high"


def test_dangerous_with_valid_confirm_token_is_allowed(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={
            "rm": ToolKnowledge(
                safety_class="mutating",
                flag_patterns=[FlagPattern(match=["-rf"], escalates_to="dangerous")],
            )
        },
    )
    target = tmp_path / "victim"
    first = checker.check_run_command(["rm", "-rf", str(target)])
    assert first.confirm_token is not None
    second = checker.check_run_command(
        ["rm", "-rf", str(target)], confirm_token=first.confirm_token
    )
    assert second.allowed is True
    assert second.safety_class == "dangerous"
    assert second.risk_level == "high"


def test_confirm_token_is_single_use(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={
            "rm": ToolKnowledge(
                safety_class="mutating",
                flag_patterns=[FlagPattern(match=["-rf"], escalates_to="dangerous")],
            )
        },
    )
    target = tmp_path / "victim"
    first = checker.check_run_command(["rm", "-rf", str(target)])
    token = first.confirm_token
    assert token is not None
    # Consume it.
    checker.check_run_command(["rm", "-rf", str(target)], confirm_token=token)
    # Re-using the same token must fail with CONFIRMATION_EXPIRED.
    third = checker.check_run_command(["rm", "-rf", str(target)], confirm_token=token)
    assert third.allowed is False
    assert third.error_code == "CONFIRMATION_EXPIRED"


def test_unknown_command_is_unknown_class_but_path_check_still_runs(
    tmp_path: Path,
) -> None:
    """A non-catalogued CLI doesn't auto-pass: cwd and bare paths are still
    validated against allowed_roots."""
    checker = _make_checker(allowed_roots=[str(tmp_path)])
    inside = checker.check_run_command(["mystery", str(tmp_path / "f")])
    assert inside.allowed is True
    assert inside.safety_class == "unknown"
    outside = checker.check_run_command(["mystery", "/etc/foo"])
    assert outside.allowed is False
    assert outside.error_code == "OUT_OF_ROOT"


def test_decision_carries_detected_paths(tmp_path: Path) -> None:
    checker = _make_checker(
        allowed_roots=[str(tmp_path)],
        tools={
            "kubectl": ToolKnowledge(
                safety_class="mutating",
                path_flags=[PathFlag(flag="-f")],
            )
        },
    )
    target = tmp_path / "manifest.yaml"
    decision = checker.check_run_command(["kubectl", "apply", "-f", str(target)], cwd=str(tmp_path))
    assert decision.allowed is True
    assert str(target) in decision.detected_paths
    assert str(tmp_path) in decision.detected_paths  # cwd appended
