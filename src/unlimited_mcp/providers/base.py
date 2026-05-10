"""Provider protocol — the minimal interface every LLM backend must satisfy.

A ``Provider`` sends a list of messages to an LLM endpoint and returns the
assistant's text reply.  Phase 1 ships ``OpenAICompatProvider`` only.
Ollama and Anthropic-direct follow in phase 2.

Exceptions
----------
Providers raise :exc:`ProviderError` subclasses; the tool layer catches them
and translates to ``JobResult.error`` codes (``PROVIDER_AUTH``,
``PROVIDER_UNAVAILABLE``).  Nothing from the network ever propagates past
the tool boundary.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Base class for LLM provider failures."""


class ProviderAuthError(ProviderError):
    """HTTP 401 or 403: API key is missing, invalid, or lacks permission."""


class ProviderUnavailableError(ProviderError):
    """HTTP 5xx, network error, or timeout.  The call is safe to retry."""


@runtime_checkable
class Provider(Protocol):
    """Structural protocol for LLM completion backends.

    Implementors need not declare inheritance; mypy validates conformance
    structurally.
    """

    @property
    def name(self) -> str:
        """Unique, human-readable identifier (e.g. ``"openai_compat"``)."""
        ...

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> str:
        """Send *messages* to the LLM and return the reply text.

        Parameters
        ----------
        messages:
            Conversation history in ``{"role": ..., "content": ...}`` format.
        model:
            Model override.  When ``None`` the provider uses its default.
        max_tokens:
            Maximum tokens in the completion.
        timeout_seconds:
            Hard network timeout.  Raises :exc:`ProviderUnavailableError`
            when exceeded.
        """
        ...
