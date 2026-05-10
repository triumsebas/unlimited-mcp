"""Unit tests for :mod:`unlimited_mcp.observability.logging`."""

from __future__ import annotations

import json
import logging as stdlib_logging
from pathlib import Path

import pytest

from unlimited_mcp.observability.logging import (
    LOG_PROMPTS_ENV,
    configure_logging,
    get_logger,
    log_prompts_enabled,
)


def _flush_handlers() -> None:
    for h in list(stdlib_logging.getLogger().handlers):
        h.flush()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def test_configure_creates_logfile(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path)
    assert log_path == tmp_path / "server.jsonl"
    assert log_path.exists()


def test_logged_event_is_valid_jsonl_with_expected_keys(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path)
    logger = get_logger("test")
    logger.info(
        "tool_call",
        tool="run_command",
        job_id="abc-123",
        duration_ms=42,
    )
    _flush_handlers()

    lines = _read_jsonl(log_path)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "tool_call"
    assert line["tool"] == "run_command"
    assert line["job_id"] == "abc-123"
    assert line["duration_ms"] == 42
    assert line["level"] == "info"
    assert "timestamp" in line


def test_repeated_configure_is_idempotent(tmp_path: Path) -> None:
    """Reconfiguring removes prior handlers cleanly so we don't double-log."""
    log_path = configure_logging(tmp_path)
    log_path = configure_logging(tmp_path)  # again
    logger = get_logger("test")
    logger.info("once")
    _flush_handlers()
    lines = _read_jsonl(log_path)
    # Exactly one event written, not two.
    assert len(lines) == 1
    assert lines[0]["event"] == "once"


def test_log_level_filtering(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path, level="WARNING")
    logger = get_logger("test")
    logger.info("ignored")
    logger.warning("kept")
    _flush_handlers()
    events = [line["event"] for line in _read_jsonl(log_path)]
    assert "ignored" not in events
    assert "kept" in events


def test_bound_context_appears(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path)
    logger = get_logger("test").bind(agent="aider_local", host="local")
    logger.info("delegated")
    _flush_handlers()
    line = _read_jsonl(log_path)[0]
    assert line["agent"] == "aider_local"
    assert line["host"] == "local"


def test_log_prompts_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_PROMPTS_ENV, raising=False)
    assert log_prompts_enabled() is False


def test_log_prompts_enabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_PROMPTS_ENV, "1")
    assert log_prompts_enabled() is True


def test_log_prompts_other_values_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the literal "1" enables prompt logging — guards against truthy
    misreadings of "0", "false", etc."""
    for v in ("0", "false", "true", "yes", ""):
        monkeypatch.setenv(LOG_PROMPTS_ENV, v)
        assert log_prompts_enabled() is False, f"failed for {v!r}"


def test_logfile_contains_one_line_per_event(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path)
    logger = get_logger("test")
    logger.info("a")
    logger.info("b")
    logger.info("c")
    _flush_handlers()
    lines = _read_jsonl(log_path)
    assert [line["event"] for line in lines] == ["a", "b", "c"]
