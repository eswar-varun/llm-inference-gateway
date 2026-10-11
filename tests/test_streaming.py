from __future__ import annotations

from helpers import chat_body, make_config, sse_events

from llmgateway.api import _sse_body
from llmgateway.observability import AccessLog
from llmgateway.service import ChatService, StreamResult
from mockllm import MockConfig


def _content(events) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for event in events if isinstance(event, dict)
        for choice in event["choices"]
    )


async def test_stream_relays_tokens_then_done(gateway, mock_llm):
    response = await gateway.post("/v1/chat/completions", json=chat_body(stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert events[-1] == "[DONE]"
    assert _content(events) == "the quick brown fox jumps"
    assert events[-2]["choices"][0]["finish_reason"] == "stop"


async def test_stream_asks_upstream_for_usage_but_hides_it_unless_requested(gateway, mock_llm):
    response = await gateway.post("/v1/chat/completions", json=chat_body(stream=True))

    sent = mock_llm.state.requests[-1]["body"]
    assert sent["stream_options"] == {"include_usage": True}
    assert not any(isinstance(e, dict) and e.get("usage") for e in sse_events(response.text))


async def test_stream_includes_usage_chunk_when_client_asks(gateway):
    response = await gateway.post(
        "/v1/chat/completions",
        json=chat_body(stream=True, stream_options={"include_usage": True}),
    )

    events = sse_events(response.text)
    usage_chunk = events[-2]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["completion_tokens"] == 5


async def test_stream_usage_can_be_disabled_per_provider(make_gateway, mock_llm):
    gateway = await make_gateway(make_config(stream_usage=False))

    await gateway.post("/v1/chat/completions", json=chat_body(stream=True))

    assert "stream_options" not in mock_llm.state.requests[-1]["body"]


async def test_closing_the_response_body_closes_the_upstream():
    # The other disconnect path (see test_integration.py for the first): some ASGI servers
    # notice a hang-up only when a write fails, and then close the body generator at its
    # `yield`. Nothing is cancelled inside the relay, so the SSE body must close it itself.
    upstream_closed = []

    async def upstream():
        try:
            for i in range(1000):
                yield {"choices": [{"index": 0, "delta": {"content": str(i)}}]}
        finally:
            upstream_closed.append(True)

    gen = upstream()
    stream = StreamResult("mock", "mock-1", chunks=gen)
    stream.chunks = ChatService._relay(stream, await anext(gen), gen, client_wants_usage=False)
    body = _sse_body(stream, AccessLog())

    await anext(body)
    await body.aclose()

    assert upstream_closed == [True]


async def test_upstream_failure_before_first_token_gets_a_real_status(gateway, mock_llm):
    mock_llm.state.config = MockConfig(error_rate=1.0, error_status=503)

    response = await gateway.post("/v1/chat/completions", json=chat_body(stream=True))

    # Not a 200 with a broken stream: the client can tell this failed from the status alone.
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_server"
