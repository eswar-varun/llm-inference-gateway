"""Request handling independent of HTTP: pick a target, call it, account for usage.

The API layer turns HTTP into ChatCompletionRequest and results back into HTTP; everything
the gateway *decides* lives here, which is where retries, caching and rate limiting will slot
in later.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from .router import Router, Target
from .schemas import ChatCompletionRequest, Usage


@dataclass
class CompletionResult:
    body: dict[str, Any]
    provider: str
    upstream_model: str
    usage: Usage | None


@dataclass
class StreamResult:
    provider: str
    upstream_model: str
    chunks: AsyncGenerator[dict[str, Any], None] = field(repr=False)
    # Filled in as the stream is consumed; complete once `chunks` is exhausted.
    usage: Usage | None = None


class ChatService:
    def __init__(self, router: Router) -> None:
        self._router = router

    async def complete(self, request: ChatCompletionRequest) -> CompletionResult:
        target = self._router.targets(request.model)[0]
        body = await target.provider.complete(build_payload(request, target))
        return CompletionResult(body, target.provider.name, target.model, Usage.from_body(body))

    async def open_stream(self, request: ChatCompletionRequest) -> StreamResult:
        """Start an upstream stream and wait for its first chunk before returning.

        Waiting means connection errors and upstream 4xx/5xx surface here, while the gateway
        can still answer with a real HTTP status. Once the client has received a 200 and the
        first bytes, the only way left to report a failure is in-band.
        """
        target = self._router.targets(request.model)[0]
        upstream = target.provider.stream(build_payload(request, target))
        first = await anext(upstream, None)
        result = StreamResult(target.provider.name, target.model, chunks=_empty())
        result.chunks = self._relay(result, first, upstream, _wants_usage(request))
        return result

    @staticmethod
    async def _relay(
        result: StreamResult,
        first: dict[str, Any] | None,
        upstream: AsyncGenerator[dict[str, Any], None],
        client_wants_usage: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with aclosing(upstream):
            chunk = first
            while chunk is not None:
                usage = Usage.from_body(chunk)
                if usage is not None:
                    result.usage = usage
                # We always ask upstream for usage (we need it for accounting) but only pass
                # the usage-only chunk on if the client asked for it, as OpenAI would.
                if client_wants_usage or not _is_usage_only(chunk):
                    yield chunk
                chunk = await anext(upstream, None)


def build_payload(request: ChatCompletionRequest, target: Target) -> dict[str, Any]:
    # exclude_unset keeps explicit nulls the client sent and every unknown field, but
    # doesn't invent defaults the client never asked for.
    payload = request.model_dump(exclude_unset=True)
    payload["model"] = target.model
    payload["stream"] = request.stream
    if request.stream and target.provider.config.stream_usage:
        options = payload.get("stream_options")
        payload["stream_options"] = {**(options if isinstance(options, dict) else {}),
                                     "include_usage": True}
    return payload


def _wants_usage(request: ChatCompletionRequest) -> bool:
    options = (request.model_extra or {}).get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


def _is_usage_only(chunk: dict[str, Any]) -> bool:
    return chunk.get("choices") == [] and bool(chunk.get("usage"))


async def _empty() -> AsyncGenerator[dict[str, Any], None]:
    return
    yield
