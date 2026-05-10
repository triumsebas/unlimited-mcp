"""Unit tests for :class:`unlimited_mcp.config.loader.ConfigStore`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap

from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import Config


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    cfg = store.get()
    assert isinstance(cfg, Config)
    assert cfg.allowed_roots == []
    assert cfg.safety.allow_shell_like_argv is False
    assert cfg.agents == {}


def test_load_valid_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "schema_version: 1\n"
        "allowed_roots:\n"
        "  - /tmp/unlimited-mcp\n"
        "safety:\n"
        "  allow_shell_like_argv: false\n"
        "agents:\n"
        "  aider_local:\n"
        "    cli: aider\n"
        "    cost_tier: 1\n"
        "    params:\n"
        "      git: false\n"
        "      model: openai/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    store = ConfigStore(cfg_path)
    cfg = store.get()
    assert cfg.allowed_roots == ["/tmp/unlimited-mcp"]
    assert "aider_local" in cfg.agents
    assert cfg.agents["aider_local"].cli == "aider"
    assert cfg.agents["aider_local"].params["git"] is False


def test_unknown_top_level_key_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("schema_version: 1\nunknown_top_level_key: 42\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    with pytest.raises(ValidationError):
        store.get()


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    with pytest.raises(ValueError, match="top-level YAML must be a mapping"):
        store.get()


def test_mtime_cache_returns_same_instance(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("schema_version: 1\nallowed_roots: [/a]\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    cfg1 = store.get()
    cfg2 = store.get()
    # Same Config instance — cache is hit.
    assert cfg1 is cfg2


def test_mtime_change_triggers_reload(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("schema_version: 1\nallowed_roots: [/a]\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    cfg1 = store.get()
    assert cfg1.allowed_roots == ["/a"]

    # Sleep ≥1ms to guarantee a different mtime_ns on filesystems that round.
    time.sleep(0.01)
    cfg_path.write_text("schema_version: 1\nallowed_roots: [/b]\n", encoding="utf-8")

    cfg2 = store.get()
    assert cfg2.allowed_roots == ["/b"]
    assert cfg1 is not cfg2


def test_update_preserves_comments(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "# top-level comment, must survive\n"
        "schema_version: 1\n"
        "agents:\n"
        "  # describe aider here\n"
        "  aider_local:\n"
        "    cli: aider\n"
        "    params:\n"
        "      git: false\n"
        "      model: foo/bar\n",
        encoding="utf-8",
    )
    store = ConfigStore(cfg_path)

    def enable_git(doc: CommentedMap) -> None:
        doc["agents"]["aider_local"]["params"]["git"] = True

    cfg = store.update(enable_git)
    assert cfg.agents["aider_local"].params["git"] is True

    text = cfg_path.read_text(encoding="utf-8")
    assert "# top-level comment, must survive" in text
    assert "# describe aider here" in text
    assert "git: true" in text


def test_update_rolls_back_on_invalid(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    original = cfg_path.read_text(encoding="utf-8")

    def break_it(doc: CommentedMap) -> None:
        doc["agents"]["bad"] = {
            "cli": "x",
            "definitely_not_a_known_field": True,
        }

    with pytest.raises(ValidationError):
        store.update(break_it)
    # File untouched on disk because validation failed before write.
    assert cfg_path.read_text(encoding="utf-8") == original


def test_atomic_write_leaves_no_partial_files(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    store = ConfigStore(cfg_path)

    def add_agent(doc: CommentedMap) -> None:
        doc.setdefault("agents", CommentedMap())
        doc["agents"]["aider_local"] = {"cli": "aider", "cost_tier": 1}

    store.update(add_agent)
    leftovers = list(tmp_path.glob(".config.yaml.*.tmp"))
    assert leftovers == []


def test_update_creates_file_when_missing(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    assert not cfg_path.exists()
    store = ConfigStore(cfg_path)

    def add_root(doc: CommentedMap) -> None:
        doc["allowed_roots"] = ["/tmp/unlimited-mcp"]

    cfg = store.update(add_root)
    assert cfg.allowed_roots == ["/tmp/unlimited-mcp"]
    assert cfg_path.exists()
