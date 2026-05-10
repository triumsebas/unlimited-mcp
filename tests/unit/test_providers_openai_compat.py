"""Unit tests for providers/base.py (Provider protocol) and
providers/openai_compat.py (OpenAICompatProvider with httpx mock transport).

No real network calls are made.  Tests inject a custom httpx transport so
the provider's HTTP logic is exercised without leaving localhost.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from unlimited_mcp.providers import (
    OPENCODE_BASE_URL,
    OPENCODE_DEFAULT_MODEL,
    OpenAICompatProvider,
    Provider,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
)

# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------

_Handler = Callable[[httpx.Request], httpx.Response]


class _MockTransport(httpx.BaseTransport):
    """Intercepts all httpx requests and delegates to a handler callable."""

    def __init__(self, handler: _Handler) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def _client_for(handler: _Handler) -> httpx.Client:
    return httpx.Client(transport=_MockTransport(handler))


def _ok_response(content: str = "summary text", model: str = "test-model") -> httpx.Response:
    body = {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }
    return httpx.Response(200, json=body)


def _provider(
    handler: _Handler,
    *,
    default_model: str = "test-model",
    name: str = "test_provider",
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=OPENCODE_BASE_URL,
        api_key="test-key",
        default_model=default_model,
        name=name,
        http_client=_client_for(handler),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_provider_protocol() -> None:
    p = _provider(lambda _: _ok_response())
    assert isinstance(p, Provider)


def test_name_property() -> None:
    p = _provider(lambda _: _ok_response(), name="my_provider")
    assert p.name == "my_provider"


def test_default_name() -> None:
    p = OpenAICompatProvider(
        base_url=OPENCODE_BASE_URL,
        api_key="k",
        default_model="m",
        http_client=_client_for(lambda _: _ok_response()),
    )
    assert p.name == "openai_compat"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_returns_content() -> None:
    p = _provider(lambda _: _ok_response("hello world"))
    result = p.complete([{"role": "user", "content": "Summarise this."}])
    assert result == "hello world"


def test_complete_sends_correct_payload() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response()

    p = _provider(handler, default_model="mymodel")
    p.complete(
        [{"role": "user", "content": "hello"}],
        max_tokens=512,
    )

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body["model"] == "mymodel"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 512


def test_complete_model_override() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response()

    p = _provider(handler, default_model="default-model")
    p.complete([{"role": "user", "content": "hi"}], model="override-model")
    body = json.loads(captured[0].content)
    assert body["model"] == "override-model"


def test_complete_url_contains_chat_completions() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response()

    p = _provider(handler)
    p.complete([{"role": "user", "content": "hi"}])
    assert "chat/completions" in str(captured[0].url)


def test_complete_authorization_header() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response()

    provider = OpenAICompatProvider(
        base_url=OPENCODE_BASE_URL,
        api_key="my-secret-key",
        default_model="m",
        http_client=_client_for(handler),
    )
    provider.complete([{"role": "user", "content": "hi"}])
    assert captured[0].headers.get("authorization") == "Bearer my-secret-key"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_http_401_raises_auth_error() -> None:
    p = _provider(lambda _: httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(ProviderAuthError):
        p.complete([{"role": "user", "content": "hi"}])


def test_http_403_raises_auth_error() -> None:
    p = _provider(lambda _: httpx.Response(403, json={"error": "Forbidden"}))
    with pytest.raises(ProviderAuthError):
        p.complete([{"role": "user", "content": "hi"}])


def test_http_500_raises_unavailable() -> None:
    p = _provider(lambda _: httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(ProviderUnavailableError):
        p.complete([{"role": "user", "content": "hi"}])


def test_http_503_raises_unavailable() -> None:
    p = _provider(lambda _: httpx.Response(503, text="Service Unavailable"))
    with pytest.raises(ProviderUnavailableError):
        p.complete([{"role": "user", "content": "hi"}])


def test_network_error_raises_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.NetworkError("connection refused")

    p = _provider(handler)
    with pytest.raises(ProviderUnavailableError):
        p.complete([{"role": "user", "content": "hi"}])


def test_timeout_raises_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    p = _provider(handler)
    with pytest.raises(ProviderUnavailableError):
        p.complete([{"role": "user", "content": "hi"}])


def test_malformed_response_raises_provider_error() -> None:
    # Response is 200 but the body is missing the expected keys
    p = _provider(lambda _: httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(ProviderError):
        p.complete([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_opencode_constants_are_strings() -> None:
    assert isinstance(OPENCODE_BASE_URL, str)
    assert OPENCODE_BASE_URL.startswith("https://")
    assert isinstance(OPENCODE_DEFAULT_MODEL, str)
