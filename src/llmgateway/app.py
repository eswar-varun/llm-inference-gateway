from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .api import router as api_router
from .config import GatewayConfig
from .errors import ErrorKind, GatewayError
from .observability import RequestIdMiddleware
from .providers import build_providers
from .router import Router
from .schemas import error_body
from .service import ChatService

logger = logging.getLogger("llmgateway")


def create_app(config: GatewayConfig, *, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """Build the gateway. Pass `http_client` to point providers at a test transport."""
    owns_client = http_client is None
    # One pooled client shared by every provider: connections (and TLS sessions) to each
    # upstream host are reused across requests instead of re-handshaking per call.
    client = http_client or httpx.AsyncClient(
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
    )
    router = Router.from_config(config, build_providers(config, client))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="LLM Inference Gateway", version=__version__, lifespan=lifespan)
    app.state.router = router
    app.state.chat = ChatService(router)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        headers = {}
        if exc.retry_after_s is not None:
            headers["Retry-After"] = str(math.ceil(exc.retry_after_s))
        if exc.provider:
            headers["x-gateway-provider"] = exc.provider
        return JSONResponse(exc.body(), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # OpenAI returns 400 with a pointer to the bad parameter; FastAPI's default is a 422
        # in its own format, which OpenAI SDKs can't parse.
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", ()) if p != "body"]
        param = ".".join(loc) or None
        message = first.get("msg", "invalid request")
        if param:
            message = f"{param}: {message}"
        return JSONResponse(
            error_body(message, "invalid_request_error", ErrorKind.INVALID_REQUEST.value, param),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return JSONResponse(error_body("internal gateway error", "api_error",
                                       ErrorKind.INTERNAL.value), status_code=500)
