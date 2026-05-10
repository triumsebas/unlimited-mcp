"""Single-use confirmation tokens with a TTL.

When the safety pipeline classifies an invocation as ``dangerous`` and
no token was supplied, it issues one. The orchestrator surfaces the
``confirm_reason`` to the user; on approval, the orchestrator re-calls
the tool with ``confirm_token=<token>``. Tokens are single-use (consumed
on success) and expire after :attr:`SafetyConfig.confirm_token_ttl_seconds`
(default 300 = 5 minutes).

The store lives in process memory: the MCP server is the trust boundary,
and we deliberately don't persist tokens across restarts (a server
restart is itself a confirmation reset, which is the conservative
behavior).
"""

from __future__ import annotations

import secrets
import time
from typing import Any


class ConfirmationStore:
    """In-memory single-use token store with TTL."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        # token → (issued_at_monotonic, payload)
        self._tokens: dict[str, tuple[float, dict[str, Any]]] = {}

    def issue(self, payload: dict[str, Any] | None = None) -> str:
        """Issue a new token, prune expired tokens, and return it."""
        token = secrets.token_urlsafe(16)
        self._tokens[token] = (time.monotonic(), dict(payload) if payload else {})
        self._gc()
        return token

    def consume(self, token: str) -> dict[str, Any] | None:
        """Single-use: return the payload if the token is valid; remove it.
        Return ``None`` if the token is unknown or expired (and remove the
        expired entry to keep the store small)."""
        self._gc()
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        issued_at, payload = entry
        if time.monotonic() - issued_at > self.ttl_seconds:
            return None
        return payload

    def __len__(self) -> int:
        return len(self._tokens)

    def _gc(self) -> None:
        now = time.monotonic()
        expired = [t for t, (issued, _) in self._tokens.items() if now - issued > self.ttl_seconds]
        for t in expired:
            del self._tokens[t]
