"""FireworksClient — every test here encodes a trap found by probing the live API."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from t2s_core.clients import FireworksClient
from t2s_core.config import FireworksConfig
from t2s_core.errors import (
    InvalidResponse,
    RateLimited,
    TransportError,
    TruncatedResponse,
    UpstreamError,
)
from t2s_core.ports import Message
from t2s_core.wire import ENVELOPE_SCHEMA

SECRET = "fw_secret_do_not_log_me"
MESSAGES = [Message("system", "you are a utility"), Message("user", "how many orders?")]


def body(content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "accounts/fireworks/models/kimi-k2p7-code",
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def client_with(handler: Any, **overrides: Any) -> FireworksClient:
    config = FireworksConfig(api_key=SecretStr(SECRET), max_retries=2, **overrides)
    return FireworksClient(
        config,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )


def call(client: FireworksClient, **kwargs: Any) -> Any:
    return client.complete(MESSAGES, response_schema=ENVELOPE_SCHEMA, **kwargs)


def test_happy_path_sends_strict_json_schema_and_returns_content() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body('{"response_class":"valid"}'))

    response = call(client_with(handler))

    assert response.content == '{"response_class":"valid"}'
    assert response.usage.total_tokens == 150
    sent = seen[0]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["temperature"] == 0.0
    assert sent["max_tokens"] == 2000


def test_truncation_is_detected_before_parsing_and_retried_with_a_bigger_budget() -> None:
    """Finding #1. The first response is *unparseable* JSON; if we had called
    json.loads first we would have reported a parse error instead of a budget
    problem."""
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.content)["max_tokens"])
        if len(budgets) == 1:
            truncated = '{"response_class": "valid", "query": "SELECT'
            return httpx.Response(200, json=body(truncated, "length"))
        return httpx.Response(200, json=body('{"response_class":"valid"}'))

    response = call(client_with(handler), max_tokens=800)

    assert response.content == '{"response_class":"valid"}'
    assert budgets == [800, 1600]


def test_persistent_truncation_raises_a_typed_failure_not_a_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body('{"response_class": "valid", "query": "SEL', "length"))

    with pytest.raises(TruncatedResponse) as caught:
        call(client_with(handler), max_tokens=800)
    assert caught.value.max_tokens == 1600


def test_429_is_retried_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "slow down", "code": "rate"}})
        return httpx.Response(200, json=body("{}"))

    assert call(client_with(handler)).content == "{}"
    assert len(calls) == 3


def test_429_past_the_budget_raises_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down", "code": "rate"}})

    with pytest.raises(RateLimited):
        call(client_with(handler))


def test_non_retryable_error_carries_request_id_and_code(caplog: Any) -> None:
    """The probed error shape: {"error": {message, code, ...}, "request_id": ...}."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {"message": "Model not found", "code": "not_found", "type": "invalid"},
                "request_id": "req-abc-123",
            },
        )

    with (
        caplog.at_level(logging.WARNING, logger="t2s_core.inference"),
        pytest.raises(UpstreamError) as caught,
    ):
        call(client_with(handler))

    assert caught.value.request_id == "req-abc-123"
    assert caught.value.code == "not_found"
    assert "req-abc-123" in caplog.text


def test_the_api_key_never_appears_in_logs_or_reprs(caplog: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(500, json={"error": {"message": "boom"}, "request_id": "r1"})

    client = client_with(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamError) as caught:
        call(client)

    assert SECRET not in caplog.text
    assert SECRET not in repr(client.config)
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(client.config.api_key)


def test_transport_failure_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TransportError):
        call(client_with(handler))


def test_empty_choices_is_an_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(InvalidResponse):
        call(client_with(handler))


def test_config_from_env_requires_a_key() -> None:
    with pytest.raises(RuntimeError, match="FIREWORKS_API_KEY"):
        FireworksConfig.from_env({})
    config = FireworksConfig.from_env({"FIREWORKS_API_KEY": SECRET, "T2S_MODEL": "m/x"})
    assert config.model == "m/x"
    assert config.api_key.get_secret_value() == SECRET
