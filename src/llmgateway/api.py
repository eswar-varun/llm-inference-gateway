from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .errors import GatewayError
from .observability import AccessLog
from .schemas import ChatCompletionRequest
from .service import ChatService, StreamResult
from .sse import encode_event

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request) -> Response:
    service: ChatService = request.app.state.chat
    access = AccessLog(model=body.model, stream=body.stream)
    try:
        if body.stream:
            return _stream_response(await service.open_stream(body), access)
        result = await service.complete(body)
    except GatewayError as exc:
        access.set(provider=exc.provider)
        access.emit(exc.http_status, error=exc.kind)
        raise

    access.set(provider=result.provider, upstream_model=result.upstream_model)
    access.emit(200, usage=result.usage)
    return JSONResponse(result.body, headers=_route_headers(result.provider, result.upstream_model))


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    aliases = request.app.state.router.aliases()
    return {
        "object": "list",
        "data": [{"id": a, "object": "model", "created": 0, "owned_by": "llm-gateway"}
                 for a in aliases],
    }


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


def _stream_response(stream: StreamResult, access: AccessLog) -> StreamingResponse:
    access.set(provider=stream.provider, upstream_model=stream.upstream_model)
    return StreamingResponse(
        _sse_body(stream, access),
        media_type="text/event-stream",
        # X-Accel-Buffering stops nginx-style proxies from batching tokens.
        headers={**_route_headers(stream.provider, stream.upstream_model),
                 "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_body(stream: StreamResult, access: AccessLog) -> AsyncIterator[bytes]:
    error: str | None = None
    try:
        async for chunk in stream.chunks:
            access.mark_first_token()
            yield encode_event(json.dumps(chunk, separators=(",", ":")))
        yield encode_event("[DONE]")
    except GatewayError as exc:
        # Headers and a 200 are already on the wire; an error event is the only signal left.
        # The OpenAI SDKs raise on it. No [DONE] follows, so the truncation is unambiguous.
        error = exc.kind
        yield encode_event(json.dumps(exc.body(), separators=(",", ":")))
    except (asyncio.CancelledError, GeneratorExit):
        error = "client_disconnected"
        raise
    finally:
        # Closing the relay closes the upstream response, so a client that hangs up stops
        # the upstream from generating (and billing) tokens nobody will read.
        await stream.chunks.aclose()
        access.emit(200, usage=stream.usage, error=error)


def _route_headers(provider: str, upstream_model: str) -> dict[str, str]:
    return {"x-gateway-provider": provider, "x-gateway-upstream-model": upstream_model}
