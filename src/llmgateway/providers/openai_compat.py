"""Provider for OpenAI-compatible APIs: OpenAI, Azure OpenAI, Ollama, vLLM, and the mock server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from ..config import ProviderConfig
from ..errors import ErrorKind, GatewayError, classify_status
from ..sse import iter_event_data
from .base import Provider


class OpenAICompatibleProvider(Provider):
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient) -> None:
        super().__init__(config)
        self._client = client
        t = config.timeout
        # pool= bounds how long we wait for a free connection from our own pool. Hitting it
        # means *we* are saturated, which is reported differently from a slow upstream.
        self._timeout = httpx.Timeout(connect=t.connect_s, read=t.read_s, write=t.read_s,
                                      pool=t.connect_s)
        self._base_url = config.base_url.rstrip("/")

    def _url(self, model: str) -> str:
        if self.config.type == "azure_openai":
            # Azure addresses a deployment in the path rather than a model in the body.
            return (f"{self._base_url}/openai/deployments/{model}/chat/completions"
                    f"?api-version={self.config.api_version}")
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.config.api_key()
        if not key:
            return {}
        if self.config.type == "azure_openai":
            return {"api-key": key}
        return {"Authorization": f"Bearer {key}"}

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            # read_s alone doesn't bound total time: an upstream trickling one byte every
            # read_s - 1 seconds never trips it. The deadline does.
            async with asyncio.timeout(self.config.timeout.total_s):
                response = await self._client.post(
                    self._url(payload["model"]), json=payload, headers=self._headers(),
                    timeout=self._timeout,
                )
        except TimeoutError as exc:
            raise self._error(ErrorKind.TIMEOUT, "upstream exceeded the request deadline",
                              retryable=True) from exc
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc

        if response.status_code >= 400:
            raise self._status_error(response)
        try:
            return response.json()
        except ValueError as exc:
            raise self._error(ErrorKind.UPSTREAM_PROTOCOL, "upstream returned invalid JSON",
                              retryable=True) from exc

    async def stream(self, payload: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
        request = self._client.build_request(
            "POST", self._url(payload["model"]), json=payload, headers=self._headers(),
            timeout=self._timeout,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc

        try:
            if response.status_code >= 400:
                await response.aread()
                raise self._status_error(response)
            async for data in iter_event_data(response.aiter_lines()):
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise self._error(ErrorKind.UPSTREAM_PROTOCOL,
                                      "upstream sent a malformed stream event") from exc
                if "error" in chunk:
                    # Some providers report failures in-band after the stream has started.
                    message = _error_message(chunk) or "upstream reported an error mid-stream"
                    raise self._error(ErrorKind.UPSTREAM_SERVER, message)
                yield chunk
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        finally:
            await response.aclose()

    def _error(self, kind: ErrorKind, message: str, **kwargs: Any) -> GatewayError:
        return GatewayError(kind, message, provider=self.name, **kwargs)

    def _transport_error(self, exc: httpx.HTTPError) -> GatewayError:
        if isinstance(exc, httpx.PoolTimeout):
            return self._error(ErrorKind.OVERLOADED, "gateway connection pool exhausted",
                               retryable=True)
        if isinstance(exc, httpx.TimeoutException):
            return self._error(ErrorKind.TIMEOUT, f"upstream timed out ({type(exc).__name__})",
                               retryable=True)
        return self._error(ErrorKind.CONNECTION,
                           f"upstream connection failed ({type(exc).__name__})", retryable=True)

    def _status_error(self, response: httpx.Response) -> GatewayError:
        kind, retryable = classify_status(response.status_code)
        detail = _error_message(_safe_json(response)) or response.text[:200]
        if kind is ErrorKind.INVALID_REQUEST:
            # The client caused this, so show them the upstream's explanation and status.
            return self._error(kind, detail or "upstream rejected the request",
                               status_code=response.status_code)
        return self._error(
            kind,
            f"upstream returned HTTP {response.status_code}" + (f": {detail}" if detail else ""),
            retryable=retryable,
            retry_after_s=_retry_after(response),
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(body: Any) -> str | None:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
    return None


def _retry_after(response: httpx.Response) -> float | None:
    # Only the delta-seconds form; HTTP-date Retry-After is rare from LLM APIs.
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None
