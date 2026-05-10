"""LLM provider backends.

Phase 1 ships :class:`OpenAICompatProvider` only.  Ollama and
Anthropic-direct follow in phase 2.
"""

from .base import Provider, ProviderAuthError, ProviderError, ProviderUnavailableError
from .openai_compat import OPENCODE_BASE_URL, OPENCODE_DEFAULT_MODEL, OpenAICompatProvider

__all__ = [
    "OPENCODE_BASE_URL",
    "OPENCODE_DEFAULT_MODEL",
    "OpenAICompatProvider",
    "Provider",
    "ProviderAuthError",
    "ProviderError",
    "ProviderUnavailableError",
]
