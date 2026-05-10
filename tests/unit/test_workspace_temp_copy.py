"""Unit tests for :mod:`unlimited_mcp.workspace.temp_copy`."""

from __future__ import annotations

from pathlib import Path

import pytest

from unlimited_mcp.workspace.temp_copy import (
    cleanup_temp_copy,
    create_temp_copy,
)


def test_create_copies_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("print('a')", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.py").write_text("print('b')", encoding="utf-8")

    target = tmp_path / "copy"
    handle = create_temp_copy(src, target)

    assert handle.path == target
    assert (target / "a.py").read_text(encoding="utf-8") == "print('a')"
    assert (target / "sub" / "b.py").read_text(encoding="utf-8") == "print('b')"


def test_create_ignores_default_patterns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "code.py").write_text("# code", encoding="utf-8")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_text("bytecode", encoding="utf-8")

    target = tmp_path / "copy"
    create_temp_copy(src, target)

    assert (target / "code.py").exists()
    assert not (target / ".git").exists()
    assert not (target / "__pycache__").exists()


def test_create_respects_custom_ignore(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.py").write_text("k", encoding="utf-8")
    (src / "drop.tmp").write_text("d", encoding="utf-8")

    target = tmp_path / "copy"
    create_temp_copy(src, target, ignore_patterns=("*.tmp",))

    assert (target / "keep.py").exists()
    assert not (target / "drop.tmp").exists()


def test_create_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_temp_copy(tmp_path / "ghost", tmp_path / "copy")


def test_create_target_already_exists_raises(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = tmp_path / "copy"
    target.mkdir()
    with pytest.raises(FileExistsError):
        create_temp_copy(src, target)


def test_cleanup_removes_target(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a", encoding="utf-8")
    target = tmp_path / "copy"

    handle = create_temp_copy(src, target)
    cleanup_temp_copy(handle)
    assert not target.exists()


def test_cleanup_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = tmp_path / "copy"
    handle = create_temp_copy(src, target)
    cleanup_temp_copy(handle)
    # Second cleanup is a no-op, not an error.
    cleanup_temp_copy(handle)
