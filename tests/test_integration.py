"""Behaviour that only shows up over real sockets.

httpx.ASGITransport buffers whole responses, so the in-process tests can't see *when* bytes
arrive or what happens when a client hangs up. These run the mock upstream and the gateway
under uvicorn on ephemeral ports.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from helpers import chat_body, make_config

from llmgateway.app import create_app
from mockllm import MockConfig, create_mock_app


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def stack(mock_config: MockConfig) -> AsyncIterator[tuple[FastAPI, str]]:
    mock = create_mock_app(mock_config)
    async with serve(mock) as mock_url, serve(create_app(make_config(f"{mock_url}/v1"))) as gw:
        yield mock, gw


async def test_tokens_are_relayed_as_they_are_generated():
    async with stack(MockConfig(tokens_per_s=40, output_tokens=12)) as (_, gw), \
            httpx.AsyncClient() as client:
        arrivals = []
        start = time.perf_counter()
        async with client.stream("POST", f"{gw}/v1/chat/completions",
                                 json=chat_body(stream=True)) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    arrivals.append(time.perf_counter() - start)

    # 11 inter-token gaps at 40 tok/s is ~275 ms. A buffering gateway would deliver every
    # event at (nearly) the same instant.
    assert arrivals[-1] - arrivals[0] > 0.2


async def test_client_hang_up_aborts_the_upstream_stream():
    async with stack(MockConfig(tokens_per_s=50, output_tokens=1000)) as (mock, gw), \
            httpx.AsyncClient() as client:
        async with client.stream("POST", f"{gw}/v1/chat/completions",
                                 json=chat_body(stream=True)) as response:
            async for i, _ in _aenumerate(response.aiter_lines()):
                if i >= 5:
                    break  # hang up after a few tokens; 1000 would take 20 s

        stats = mock.state.stats
        for _ in range(100):
            if stats["streams_aborted"]:
                break
            await asyncio.sleep(0.02)
        assert stats["streams_aborted"] == 1
        assert stats["active_streams"] == 0


async def _aenumerate(iterator):
    i = 0
    async for item in iterator:
        yield i, item
        i += 1
