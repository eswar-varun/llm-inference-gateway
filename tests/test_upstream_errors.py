"""How each kind of upstream failure reaches the client.

These use httpx.MockTransport so the upstream's exact bytes, status and failure mode are
scripted per test.
"""

from __future__ import annotations

import httpx
import pytest
from helpers import chat_body, make_config, sse_events

from mockllm import MockConfig


def _upstream(status: int = 200, *, json=None, content: bytes | None = None,
              headers: dict[str, str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=json, content=content, headers=headers)
    return httpx.MockTransport(handler)


def _raising(exc: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return httpx.MockTransport(handler)


async def test_upstream_429_passes_through_with_retry_after(make_gateway):
    gateway = await make_gateway(transport=_upstream(
        429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"}))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["type"] == "rate_limit_error"


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_client_errors_keep_upstream_status_and_message(make_gateway, status):
    gateway = await make_gateway(transport=_upstream(
        status, json={"error": {"message": "maximum context length is 8192 tokens"}}))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == status
    assert response.json()["error"]["message"] == "maximum context length is 8192 tokens"


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_upstream_auth_or_deployment_errors_are_gateway_faults(make_gateway, status):
    gateway = await make_gateway(transport=_upstream(status, json={"error": {"message": "x"}}))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    # The client did nothing wrong; our credentials or deployment name are.
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_config"


async def test_upstream_5xx_is_a_502(make_gateway):
    gateway = await make_gateway(transport=_upstream(500, content=b"oops"))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_server"


async def test_connection_failure_is_a_502(make_gateway):
    gateway = await make_gateway(transport=_raising(httpx.ConnectError("refused")))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "connection"


async def test_read_timeout_is_a_504(make_gateway):
    gateway = await make_gateway(transport=_raising(httpx.ReadTimeout("slow")))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"


async def test_exhausted_connection_pool_is_a_503_not_a_timeout(make_gateway):
    gateway = await make_gateway(transport=_raising(httpx.PoolTimeout("pool full")))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "overloaded"


async def test_total_deadline_bounds_slow_upstreams(make_gateway, mock_llm):
    mock_llm.state.config = MockConfig(ttft_ms=1000)
    gateway = await make_gateway(make_config(timeout={"total_s": 0.05}))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 504


async def test_invalid_json_from_upstream_is_a_502(make_gateway):
    gateway = await make_gateway(transport=_upstream(200, content=b"<html>proxy error</html>"))

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_protocol"


_FIRST_CHUNK = (b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},'
                b'"finish_reason":null}]}\n\n')


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        (b'data: {"error":{"message":"model overloaded"}}\n\n', "upstream_server"),
        (b"data: {not json\n\n", "upstream_protocol"),
    ],
)
async def test_mid_stream_failure_becomes_an_error_event(make_gateway, tail, code):
    gateway = await make_gateway(transport=_upstream(
        200, content=_FIRST_CHUNK + tail, headers={"content-type": "text/event-stream"}))

    response = await gateway.post("/v1/chat/completions", json=chat_body(stream=True))

    assert response.status_code == 200  # already committed when the failure happened
    events = sse_events(response.text)
    assert events[0]["choices"][0]["delta"]["content"] == "hi"
    assert events[-1]["error"]["code"] == code
    assert "[DONE]" not in events  # truncation must be unambiguous
