"""Unit tests for :class:`unlimited_mcp.config.knowledge.KnowledgeStore`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.schema import Knowledge


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


def test_both_missing_returns_empty(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.yaml", tmp_path / "local.yaml")
    k = store.get()
    assert isinstance(k, Knowledge)
    assert k.clis == {}
    assert k.tools == {}


def test_only_repo_loads(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    _write(
        repo,
        "clis:\n"
        "  aider:\n"
        "    command_template: 'aider {flags}'\n"
        "    verified: true\n"
        "tools:\n"
        "  rg:\n"
        "    safety_class: read\n",
    )
    store = KnowledgeStore(repo, tmp_path / "local.yaml")
    k = store.get()
    assert "aider" in k.clis
    assert k.clis["aider"].verified is True
    assert k.tools["rg"].safety_class == "read"


def test_only_local_loads(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    _write(
        local,
        "clis:\n  custom_cli:\n    command_template: 'custom {flags}'\n",
    )
    store = KnowledgeStore(tmp_path / "knowledge.yaml", local)
    k = store.get()
    assert "custom_cli" in k.clis


def test_merge_no_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(
        repo,
        "clis:\n  aider:\n    command_template: 'aider {flags}'\n",
    )
    _write(
        local,
        "clis:\n  opencode:\n    command_template: 'opencode {flags}'\n",
    )
    store = KnowledgeStore(repo, local)
    k = store.get()
    assert set(k.clis.keys()) == {"aider", "opencode"}


def test_local_replaces_repo_entry(tmp_path: Path) -> None:
    """Entry-level replacement: local fully replaces the same-named repo entry."""
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(
        repo,
        "clis:\n"
        "  aider:\n"
        "    command_template: 'aider {flags} --message {prompt!q}'\n"
        "    verified: true\n"
        "    verified_version: '0.x'\n",
    )
    _write(
        local,
        "clis:\n  aider:\n    command_template: 'aider --custom {flags}'\n    verified: false\n",
    )
    store = KnowledgeStore(repo, local)
    k = store.get()
    aider = k.clis["aider"]
    assert aider.command_template == "aider --custom {flags}"
    # Entry-level replacement: verified_version not declared locally, so
    # local's missing field falls back to its schema default (None) — the
    # repo's "0.x" is dropped because the entry was fully replaced.
    assert aider.verified is False
    assert aider.verified_version is None


def test_top_level_scalar_override(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(repo, "schema_version: 1\n")
    _write(local, "schema_version: 2\n")
    store = KnowledgeStore(repo, local)
    k = store.get()
    assert k.schema_version == 2


def test_get_repo_and_get_local_are_unmerged(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(repo, "clis:\n  aider:\n    command_template: 'aider'\n")
    _write(local, "clis:\n  opencode:\n    command_template: 'opencode'\n")
    store = KnowledgeStore(repo, local)
    assert set(store.get_repo().clis.keys()) == {"aider"}
    assert set(store.get_local().clis.keys()) == {"opencode"}


def test_mtime_cache_returns_same_instance(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    _write(repo, "clis:\n  aider:\n    command_template: 'aider'\n")
    store = KnowledgeStore(repo, tmp_path / "local.yaml")
    k1 = store.get()
    k2 = store.get()
    assert k1 is k2


def test_mtime_change_in_either_triggers_reload(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(repo, "clis:\n  aider:\n    command_template: 'aider {flags}'\n")
    store = KnowledgeStore(repo, local)
    k1 = store.get()
    assert "aider" in k1.clis and "opencode" not in k1.clis

    time.sleep(0.01)
    _write(local, "clis:\n  opencode:\n    command_template: 'opencode'\n")
    k2 = store.get()
    assert {"aider", "opencode"} <= set(k2.clis.keys())
    assert k1 is not k2

    # Now mutate the repo and check we still pick it up.
    time.sleep(0.01)
    _write(
        repo,
        "clis:\n  aider:\n    command_template: 'aider --new {flags}'\n",
    )
    k3 = store.get()
    assert k3.clis["aider"].command_template == "aider --new {flags}"
    assert k2 is not k3


def test_invalid_top_level_raises(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    _write(repo, "- not\n- a\n- mapping\n")
    store = KnowledgeStore(repo, tmp_path / "local.yaml")
    with pytest.raises(ValueError, match="top-level YAML must be a mapping"):
        store.get()


def test_unknown_field_raises(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    _write(repo, "totally_unknown_key: 1\n")
    store = KnowledgeStore(repo, tmp_path / "local.yaml")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        store.get()


def test_reload_forces_reread(tmp_path: Path) -> None:
    repo = tmp_path / "knowledge.yaml"
    _write(repo, "clis:\n  aider:\n    command_template: 'aider'\n")
    store = KnowledgeStore(repo, tmp_path / "local.yaml")
    k1 = store.get()
    k2 = store.reload()
    # reload() always re-parses, so we get a fresh instance even though the
    # file is unchanged.
    assert k1 is not k2
    assert k1.clis.keys() == k2.clis.keys()


def test_replacement_within_runners_dict_of_any(tmp_path: Path) -> None:
    """Runners hold opaque dicts; entry-level replacement should still apply."""
    repo = tmp_path / "knowledge.yaml"
    local = tmp_path / "local.yaml"
    _write(
        repo,
        "runners:\n  smolagents:\n    retries: 2\n    flavor: groq\n",
    )
    _write(
        local,
        "runners:\n  smolagents:\n    retries: 5\n",
    )
    store = KnowledgeStore(repo, local)
    k = store.get()
    smol = k.runners["smolagents"]
    assert smol["retries"] == 5
    # Entry was fully replaced — the repo's `flavor` key is gone.
    assert "flavor" not in smol
