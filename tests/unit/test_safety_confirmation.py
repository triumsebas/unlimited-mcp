"""Unit tests for :class:`unlimited_mcp.safety.confirmation.ConfirmationStore`."""

from __future__ import annotations

import time

import pytest

from unlimited_mcp.safety.confirmation import ConfirmationStore


def test_issue_returns_unique_token() -> None:
    store = ConfirmationStore()
    a = store.issue()
    b = store.issue()
    assert a != b
    assert isinstance(a, str)
    assert len(a) > 16  # token_urlsafe produces something readable


def test_consume_returns_payload() -> None:
    store = ConfirmationStore()
    token = store.issue({"argv": ["rm", "-rf"], "cwd": "/tmp"})
    payload = store.consume(token)
    assert payload == {"argv": ["rm", "-rf"], "cwd": "/tmp"}


def test_consume_is_single_use() -> None:
    store = ConfirmationStore()
    token = store.issue({"x": 1})
    assert store.consume(token) == {"x": 1}
    assert store.consume(token) is None  # already consumed


def test_consume_unknown_token_returns_none() -> None:
    store = ConfirmationStore()
    assert store.consume("never-issued") is None


def test_expired_token_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConfirmationStore(ttl_seconds=10)
    fake_now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    token = store.issue({"a": 1})
    fake_now[0] = 11.0  # 11s later, beyond TTL
    assert store.consume(token) is None


def test_expired_tokens_are_garbage_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConfirmationStore(ttl_seconds=5)
    fake_now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    store.issue({"a": 1})
    store.issue({"b": 2})
    assert len(store) == 2

    fake_now[0] = 10.0
    # Issuing a new token triggers GC.
    fresh = store.issue({"c": 3})
    assert len(store) == 1
    assert store.consume(fresh) == {"c": 3}


def test_zero_or_negative_ttl_rejected() -> None:
    with pytest.raises(ValueError):
        ConfirmationStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        ConfirmationStore(ttl_seconds=-1)


def test_no_payload_yields_empty_dict() -> None:
    store = ConfirmationStore()
    token = store.issue()
    assert store.consume(token) == {}


def test_payload_is_copied_not_referenced() -> None:
    """Mutating the original dict after issue must not affect the stored payload."""
    store = ConfirmationStore()
    payload: dict[str, object] = {"argv": ["rm"]}
    token = store.issue(payload)
    payload["argv"] = ["ls"]  # mutate original
    consumed = store.consume(token)
    assert consumed == {"argv": ["rm"]}
