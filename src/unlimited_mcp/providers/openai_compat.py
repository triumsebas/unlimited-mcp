"""OpenAI-compatible provider: any endpoint that speaks ``/chat/completions``.

Default target: OpenCode Go (``https://opencode.ai/zen/go/v1``,
``deepseek-v4-flash``).  Switch to Groq, OpenRouter, or a local server by
passing a different ``base_url`` and ``api_key``.

Usage
-----
::

    provider = OpenAICompatProvider(
        base_url="https://opencode.ai/zen/go/v1",
        api_key=os.environ["OPENCODE_API_KEY"],
        default_model="deepseek-v4-flash",
    )
    reply = provider.complete([{"role": "user", "content": "Summarize: ..."}])
"""

from __future__ import annotations

import httpx

from .base import ProviderAuthError, ProviderError, ProviderUnavailableError

#: OpenCode Go base URL — the default for phase 1.
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
#: Default model served by OpenCode Go.
OPENCODE_DEFAULT_MODEL = "deepseek-v4-flash"


class OpenAICompatProvider:
    """Provider that calls any OpenAI-compatible ``/chat/completions`` endpoint.

    Parameters
    ----------
    base_url:
        Root URL of the API (no trailing slash).
    api_key:
        Bearer token passed in the ``Authorization`` header.
    default_model:
        Model used when :meth:`complete` is called without an explicit model.
    name:
        Identifier for this provider instance (used in logs and ``list_capabilities``).
    http_client:
        Optional pre-built :class:`httpx.Client`.  Injected in tests to avoid
        real network calls; when ``None`` a client is created automatically.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        *,
        name: str = "openai_compat",
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model
        self._name = name
        self._client = http_client or httpx.Client()

    @property
    def name(self) -> str:
        return self._name

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> str:
        payload: dict[str, object] = {
            "model": model or self._default_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                f"Request to {self._base_url} timed out after {timeout_seconds}s"
            ) from exc
        except httpx.NetworkError as exc:
            raise ProviderUnavailableError(
                f"Network error reaching {self._base_url}: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise ProviderAuthError(
                f"HTTP {resp.status_code} from {self._base_url}: check your API key"
            )
        if resp.status_code >= 500:
            raise ProviderUnavailableError(
                f"HTTP {resp.status_code} from {self._base_url}: server error"
            )
        resp.raise_for_status()

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return str(content)
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                f"Unexpected response shape from {self._base_url}: {resp.text[:200]}"
            ) from exc
