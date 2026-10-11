from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from helpers import make_config

from llmgateway.app import create_app
from llmgateway.config import GatewayConfig
from mockllm import MockConfig, create_mock_app

GatewayFactory = Callable[..., Awaitable[httpx.AsyncClient]]


@pytest.fixture
def mock_llm() -> FastAPI:
    return create_mock_app(MockConfig(output_tokens=5, seed=0))


@pytest.fixture
async def make_gateway(mock_llm: FastAPI) -> AsyncIterator[GatewayFactory]:
    """Build a gateway client. By default the upstream is the in-process mock LLM server;
    pass `transport=` (e.g. httpx.MockTransport) to script upstream behaviour exactly."""
    clients: list[httpx.AsyncClient] = []

    async def factory(config: GatewayConfig | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
        upstream = httpx.AsyncClient(transport=transport or httpx.ASGITransport(app=mock_llm))
        app = create_app(config or make_config(), http_client=upstream)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://gateway")
        clients.extend([upstream, client])
        return client

    yield factory
    for client in clients:
        await client.aclose()


@pytest.fixture
async def gateway(make_gateway: GatewayFactory) -> httpx.AsyncClient:
    return await make_gateway()
