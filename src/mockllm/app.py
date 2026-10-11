"""A fake OpenAI-compatible LLM server with controllable latency and failures.

Real providers are slow, cost money, and fail when *they* choose to. Load tests and chaos
experiments need an upstream that fails exactly when told to, so every behaviour here is a
knob, adjustable at runtime through /admin/config.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

_WORDS = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]


class MockConfig(BaseModel):
    ttft_ms: float = Field(0, ge=0, description="Delay before the first token.")
    tokens_per_s: float = Field(0, ge=0, description="Generation speed; 0 means no delay.")
    output_tokens: int = Field(16, ge=1)
    error_rate: float = Field(0, ge=0, le=1, description="Fraction of requests that fail.")
    error_status: int = Field(503, ge=400, le=599)
    seed: int | None = None


def create_mock_app(config: MockConfig | None = None) -> FastAPI:
    app = FastAPI(title="mockllm")
    app.state.config = config or MockConfig()
    app.state.rng = random.Random(app.state.config.seed)
    # Last requests received, so tests can assert what the gateway actually sent upstream.
    app.state.requests = deque(maxlen=100)
    # streams_aborted is how we prove a client hang-up propagates through the gateway.
    app.state.stats = {"requests": 0, "active_streams": 0, "streams_completed": 0,
                       "streams_aborted": 0}

    async def chat(request: Request, deployment: str | None = None) -> Response:
        cfg: MockConfig = app.state.config
        body = await request.json()
        app.state.stats["requests"] += 1
        app.state.requests.append({"path": request.url.path, "query": str(request.url.query),
                                   "headers": dict(request.headers), "body": body})

        if app.state.rng.random() < cfg.error_rate:
            return JSONResponse(
                {"error": {"message": f"mock failure ({cfg.error_status})",
                           "type": "server_error", "code": None}},
                status_code=cfg.error_status,
            )

        model = deployment or body.get("model", "mock")
        words = [_WORDS[i % len(_WORDS)] for i in range(cfg.output_tokens)]
        usage = {
            "prompt_tokens": _count_prompt_tokens(body.get("messages", [])),
            "completion_tokens": len(words),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"

        if body.get("stream"):
            options = body.get("stream_options") or {}
            return StreamingResponse(
                _tracked(app.state.stats,
                         _stream(cfg, completion_id, model, words,
                                 usage if options.get("include_usage") else None)),
                media_type="text/event-stream",
            )

        await asyncio.sleep(_generation_time_s(cfg, len(words)))
        return JSONResponse({
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": " ".join(words)},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })

    app.add_api_route("/v1/chat/completions", chat, methods=["POST"])
    # Azure-style path, so the azure_openai provider can be exercised end to end.
    app.add_api_route("/openai/deployments/{deployment}/chat/completions", chat, methods=["POST"])

    @app.get("/admin/config")
    async def get_config() -> MockConfig:
        return app.state.config

    @app.put("/admin/config")
    async def put_config(new: MockConfig) -> MockConfig:
        app.state.config = new
        app.state.rng = random.Random(new.seed)
        return new

    @app.get("/admin/stats")
    async def get_stats() -> dict[str, int]:
        return app.state.stats

    return app


async def _tracked(stats: dict[str, int], events: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    stats["active_streams"] += 1
    finished = False
    try:
        async for event in events:
            yield event
        finished = True
    finally:
        stats["active_streams"] -= 1
        stats["streams_completed" if finished else "streams_aborted"] += 1


async def _stream(cfg: MockConfig, completion_id: str, model: str, words: list[str],
                  usage: dict[str, int] | None) -> AsyncIterator[bytes]:
    created = int(time.time())

    def event(choices: list[dict[str, Any]], **extra: Any) -> bytes:
        chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                 "model": model, "choices": choices, **extra}
        return f"data: {json.dumps(chunk)}\n\n".encode()

    await asyncio.sleep(cfg.ttft_ms / 1000)
    yield event([{"index": 0, "delta": {"role": "assistant", "content": ""},
                  "finish_reason": None}])
    for i, word in enumerate(words):
        if i and cfg.tokens_per_s:
            await asyncio.sleep(1 / cfg.tokens_per_s)
        yield event([{"index": 0, "delta": {"content": word if i == 0 else f" {word}"},
                      "finish_reason": None}])
    yield event([{"index": 0, "delta": {}, "finish_reason": "stop"}])
    if usage is not None:
        yield event([], usage=usage)
    yield b"data: [DONE]\n\n"


def _generation_time_s(cfg: MockConfig, n_tokens: int) -> float:
    per_token = (n_tokens - 1) / cfg.tokens_per_s if cfg.tokens_per_s else 0.0
    return cfg.ttft_ms / 1000 + per_token


def _count_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    # Whitespace "tokens": good enough for a mock; real tokenizers arrive with accounting.
    return sum(len(str(m.get("content") or "").split()) for m in messages)
