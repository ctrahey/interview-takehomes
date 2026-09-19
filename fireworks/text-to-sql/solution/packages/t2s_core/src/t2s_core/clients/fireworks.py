"""The production :class:`~t2s_core.ports.InferenceClient`.

Fireworks is OpenAI-compatible, so this is a thin, explicit HTTP client rather
than an SDK. Three behaviours here are not negotiable, and each one came from a
live probe:

* ``response_format`` carries the JSON schema with ``strict: true`` (D7). We
  never ask for JSON in prose and never regex a code fence out of free text.
* ``finish_reason`` is checked **before** ``json.loads`` (finding #1). A
  truncated completion is invalid JSON, not a schema violation, so it gets its
  own typed failure and one retry with a larger budget.
* ``request_id`` from the error body is logged; the API key never is (D9).
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from t2s_core.config import FireworksConfig
from t2s_core.errors import (
    InvalidResponse,
    RateLimited,
    TransportError,
    TruncatedResponse,
    UpstreamError,
)
from t2s_core.models import Usage
from t2s_core.ports import InferenceResponse, Message
from t2s_core.wire import response_format

__all__ = ["FireworksClient"]

logger = logging.getLogger("t2s_core.inference")

HTTP_ERROR_FLOOR = 400
HTTP_TOO_MANY_REQUESTS = 429
_RETRY_STATUSES = frozenset({408, 409, HTTP_TOO_MANY_REQUESTS, 500, 502, 503, 504})


class FireworksClient:
    def __init__(
        self,
        config: FireworksConfig | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config or FireworksConfig.from_env()
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(self.config.timeout_s, connect=self.config.connect_timeout_s)
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self.config.model

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> FireworksClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -----------------------------------------------------------------------
    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        budget = max_tokens or self.config.max_tokens
        try:
            return self._complete_once(
                messages,
                response_schema,
                schema_name,
                max_tokens=budget,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
        except TruncatedResponse as first:
            escalated = int(budget * self.config.truncation_retry_factor)
            logger.warning(
                "completion truncated at max_tokens=%d; retrying once at %d (request_id=%s)",
                budget,
                escalated,
                first.request_id,
            )
            return self._complete_once(
                messages,
                response_schema,
                schema_name,
                max_tokens=escalated,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )

    # -----------------------------------------------------------------------
    def _complete_once(
        self,
        messages: Sequence[Message],
        response_schema: Mapping[str, Any],
        schema_name: str,
        *,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.as_wire() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format(dict(response_schema), schema_name),
        }
        if reasoning_effort is not None:
            # Measured on the router call: this model spends ~1970 reasoning
            # tokens classifying a compound utterance and expands to fill
            # whatever budget it is given, so raising max_tokens alone does not
            # stop the truncation. "none" answers the same classification in
            # 111 tokens. Generation calls leave this unset and keep reasoning.
            payload["reasoning_effort"] = reasoning_effort
        started = time.perf_counter()
        response = self._post_with_retries(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse(response, latency_ms, max_tokens)

    def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = TransportError(f"transport failure calling Fireworks: {exc}")
                if attempt == self.config.max_retries:
                    raise last_error from exc
                self._backoff(attempt, None)
                continue

            if response.status_code < HTTP_ERROR_FLOOR:
                return response

            request_id, code, message = _error_fields(response)
            logger.warning(
                "fireworks error status=%s code=%s request_id=%s attempt=%d/%d: %s",
                response.status_code,
                code,
                request_id,
                attempt + 1,
                self.config.max_retries + 1,
                message,
            )
            if response.status_code not in _RETRY_STATUSES or attempt == self.config.max_retries:
                is_rate_limit = response.status_code == HTTP_TOO_MANY_REQUESTS
                error_cls = RateLimited if is_rate_limit else UpstreamError
                raise error_cls(
                    f"Fireworks returned HTTP {response.status_code}: {message}",
                    request_id=request_id,
                    code=code,
                )
            self._backoff(attempt, response.headers.get("retry-after"))
        raise last_error or TransportError("exhausted retries without a response")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                self._sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        # Exponential with full jitter, so concurrent eval workers do not
        # synchronise into a thundering herd on a 429.
        self._sleep(random.uniform(0.0, min(2.0**attempt * 0.5, 8.0)))  # noqa: S311

    def _parse(
        self, response: httpx.Response, latency_ms: int, max_tokens: int
    ) -> InferenceResponse:
        request_id = response.headers.get("x-request-id")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise InvalidResponse(
                "Fireworks returned a non-JSON body", request_id=request_id
            ) from exc
        request_id = body.get("request_id") or request_id
        choices = body.get("choices") or []
        if not choices:
            raise InvalidResponse("Fireworks returned no choices", request_id=request_id)
        choice = choices[0]
        finish_reason = str(choice.get("finish_reason") or "")

        # BEFORE json.loads, always. Finding #1.
        if finish_reason == "length":
            raise TruncatedResponse(
                f"completion hit the {max_tokens}-token budget and was cut off",
                max_tokens=max_tokens,
                request_id=request_id,
            )

        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise InvalidResponse(
                f"Fireworks returned an empty message (finish_reason={finish_reason!r})",
                request_id=request_id,
            )
        raw_usage = body.get("usage") or {}
        return InferenceResponse(
            content=content,
            model=str(body.get("model") or self.config.model),
            finish_reason=finish_reason,
            usage=Usage.model_validate(raw_usage),
            latency_ms=latency_ms,
            request_id=request_id,
        )


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None, str]:
    """Pull ``request_id`` and ``error.code`` out of an error body (probed shape)."""
    request_id = response.headers.get("x-request-id")
    code: str | None = None
    message = response.text[:500]
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return request_id, code, message
    if isinstance(body, dict):
        request_id = body.get("request_id") or request_id
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            message = str(error.get("message") or message)
    return request_id, code, message
