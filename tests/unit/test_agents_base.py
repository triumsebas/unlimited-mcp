"""Unit tests for agents/base.py — CLIAgent.from_config + render_argv.

The tests construct ``Config`` and ``Knowledge`` instances directly via the
pydantic schemas; this avoids YAML round-tripping while still exercising
the merge precedence and template/param render contracts.
"""

from __future__ import annotations

from typing import Any

import pytest

from unlimited_mcp.agents.base import (
    AgentRenderError,
    CLIAgent,
    _expand_template,
    _substitute_value,
)
from unlimited_mcp.config.schema import (
    AgentConfig,
    CliKnowledge,
    Config,
    Knowledge,
    ParamSpec,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _aider_knowledge() -> CliKnowledge:
    """Plausible aider entry covering bool/str/list[str]/required."""
    return CliKnowledge(
        command_template="aider --message {prompt} {files}",
        params={
            "git": ParamSpec(
                type="bool",
                default=True,
                render={"true": "--git", "false": "--no-git"},
            ),
            "model": ParamSpec(
                type="str",
                default=None,
                required=True,
                render="--model {value}",
            ),
            "yes": ParamSpec(
                type="bool",
                default=True,
                render={"true": "--yes"},
            ),
            "read": ParamSpec(
                type="list[str]",
                default=None,
                render="--read {value}",
            ),
            # metadata-only param, never rendered
            "label": ParamSpec(type="str", default=None, render=""),
        },
    )


def _make_config(agents: dict[str, dict[str, Any]]) -> Config:
    return Config(agents={name: AgentConfig(**cfg) for name, cfg in agents.items()})


def _make_knowledge(clis: dict[str, CliKnowledge]) -> Knowledge:
    return Knowledge(clis=clis)


# ---------------------------------------------------------------------------
# from_config — error paths
# ---------------------------------------------------------------------------


def test_from_config_unknown_agent() -> None:
    cfg = _make_config({})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    with pytest.raises(AgentRenderError, match="Unknown agent"):
        CLIAgent.from_config("missing", cfg, kn)


def test_from_config_unknown_cli() -> None:
    cfg = _make_config({"my_agent": {"cli": "ghost", "params": {"model": "x"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    with pytest.raises(AgentRenderError, match="unknown CLI"):
        CLIAgent.from_config("my_agent", cfg, kn)


# ---------------------------------------------------------------------------
# from_config — merge precedence
# ---------------------------------------------------------------------------


def test_merge_knowledge_default_when_config_silent() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    # `git` not set in config → falls back to knowledge default (True)
    assert agent.params_active["git"] is True


def test_merge_config_overrides_knowledge_default() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"git": False, "model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    assert agent.params_active["git"] is False


def test_render_per_call_override_beats_config() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"git": False, "model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", params_override={"git": True}).argv
    assert "--git" in argv
    assert "--no-git" not in argv


# ---------------------------------------------------------------------------
# render_argv — template expansion
# ---------------------------------------------------------------------------


def test_template_substitutes_prompt_as_single_token() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="add docstrings to all functions").argv
    # Multi-word prompt stays one argv token
    assert "add docstrings to all functions" in argv


def test_template_expands_files() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", files=["foo.py", "bar.py"]).argv
    assert "foo.py" in argv
    assert "bar.py" in argv


def test_template_empty_files_collapses() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", files=[]).argv
    assert all("{files}" not in tok for tok in argv)


def test_template_prompt_required_when_referenced() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m1"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    with pytest.raises(AgentRenderError, match=r"references .*prompt"):
        agent.render_argv(prompt=None)


def test_template_cwd_substitution() -> None:
    kn = _make_knowledge(
        {
            "tool": CliKnowledge(
                command_template="mytool --in {cwd}",
                params={},
            )
        }
    )
    cfg = _make_config({"a": {"cli": "tool", "params": {}}})
    agent = CLIAgent.from_config("a", cfg, kn)
    assert agent.render_argv(cwd="/work").argv == ["mytool", "--in", "/work"]


def test_template_cwd_skipped_when_none() -> None:
    kn = _make_knowledge({"tool": CliKnowledge(command_template="mytool {cwd}", params={})})
    cfg = _make_config({"a": {"cli": "tool", "params": {}}})
    agent = CLIAgent.from_config("a", cfg, kn)
    assert agent.render_argv().argv == ["mytool"]


# ---------------------------------------------------------------------------
# render_argv — required params
# ---------------------------------------------------------------------------


def test_required_param_missing_raises() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    with pytest.raises(AgentRenderError, match="Required param 'model'"):
        agent.render_argv(prompt="hi")


def test_required_param_satisfied_by_override() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", params_override={"model": "gpt-4o"}).argv
    assert "--model" in argv
    assert "gpt-4o" in argv


# ---------------------------------------------------------------------------
# render_argv — bool params
# ---------------------------------------------------------------------------


def test_bool_true_renders_true_branch() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", params_override={"git": True}).argv
    assert "--git" in argv
    assert "--no-git" not in argv


def test_bool_false_renders_false_branch() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi", params_override={"git": False}).argv
    assert "--no-git" in argv
    assert "--git" not in argv


def test_bool_one_sided_render_skips_when_missing_key() -> None:
    """The 'yes' param has only a 'true' key — False renders nothing."""
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m", "yes": False}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi").argv
    assert "--yes" not in argv


# ---------------------------------------------------------------------------
# render_argv — scalar / list params
# ---------------------------------------------------------------------------


def test_scalar_str_param_renders_two_tokens() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "openai/gpt-4o"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi").argv
    i = argv.index("--model")
    assert argv[i + 1] == "openai/gpt-4o"


def test_list_param_repeats_per_element() -> None:
    cfg = _make_config(
        {
            "a": {
                "cli": "aider",
                "params": {"model": "m", "read": ["a.md", "b.md"]},
            }
        }
    )
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi").argv
    # Two --read flags, one per file
    assert argv.count("--read") == 2
    assert "a.md" in argv and "b.md" in argv


def test_metadata_only_param_never_rendered() -> None:
    cfg = _make_config({"a": {"cli": "aider", "params": {"model": "m", "label": "ignored"}}})
    kn = _make_knowledge({"aider": _aider_knowledge()})
    agent = CLIAgent.from_config("a", cfg, kn)
    argv = agent.render_argv(prompt="hi").argv
    assert "ignored" not in argv
    assert "label" not in argv


# ---------------------------------------------------------------------------
# render_argv — render-shape errors
# ---------------------------------------------------------------------------


def test_bool_with_non_dict_render_raises() -> None:
    kn = _make_knowledge(
        {
            "x": CliKnowledge(
                command_template="x",
                params={"flag": ParamSpec(type="bool", default=True, render="--bad")},
            )
        }
    )
    cfg = _make_config({"a": {"cli": "x", "params": {}}})
    agent = CLIAgent.from_config("a", cfg, kn)
    with pytest.raises(AgentRenderError, match="bool type requires dict"):
        agent.render_argv()


def test_list_with_non_str_render_raises() -> None:
    kn = _make_knowledge(
        {
            "x": CliKnowledge(
                command_template="x",
                params={"items": ParamSpec(type="list[str]", default=["a"], render={"x": "y"})},
            )
        }
    )
    cfg = _make_config({"a": {"cli": "x", "params": {}}})
    agent = CLIAgent.from_config("a", cfg, kn)
    with pytest.raises(AgentRenderError, match=r"list.*requires str render"):
        agent.render_argv()


def test_list_with_non_list_value_raises() -> None:
    kn = _make_knowledge(
        {
            "x": CliKnowledge(
                command_template="x",
                params={
                    "items": ParamSpec(type="list[str]", default=None, render="--item {value}")
                },
            )
        }
    )
    cfg = _make_config({"a": {"cli": "x", "params": {"items": "not-a-list"}}})
    agent = CLIAgent.from_config("a", cfg, kn)
    with pytest.raises(AgentRenderError, match="expected list"):
        agent.render_argv()


# ---------------------------------------------------------------------------
# Internal helpers (smoke tests so refactors are guarded)
# ---------------------------------------------------------------------------


def test_substitute_value_simple() -> None:
    assert _substitute_value("--model {value}", "x") == ["--model", "x"]


def test_substitute_value_combined() -> None:
    assert _substitute_value("--model={value}", "x") == ["--model=x"]


def test_expand_template_files_only() -> None:
    out = _expand_template(
        "cmd {files}", prompt=None, files=["a", "b"], cwd=None, include_prompt_token=False
    )
    assert out == ["cmd", "a", "b"]
