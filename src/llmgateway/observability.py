"""Request IDs and one structured access-log line per request.

Milestone 5 replaces most of this with OpenTelemetry traces and Prometheus metrics; the
access log stays because it is what you grep at 3 a.m.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .schemas import Usage

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
access_logger = logging.getLogger("llmgateway.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestIdMiddleware:
    """Accept the caller's X-Request-ID (if sane) or mint one, and echo it on the response.

    Written as raw ASGI rather than BaseHTTPMiddleware so it never buffers streamed bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


class AccessLog:
    """Collects fields over a request's lifetime and emits them as one JSON line."""

    def __init__(self, **fields: Any) -> None:
        self._start = time.perf_counter()
        self._first_token: float | None = None
        self.fields: dict[str, Any] = {"request_id": request_id_var.get(), **fields}

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)

    def mark_first_token(self) -> None:
        if self._first_token is None:
            self._first_token = time.perf_counter()

    def emit(self, status: int, usage: Usage | None = None, error: str | None = None) -> None:
        record = {
            **self.fields,
            "status": status,
            "latency_ms": round((time.perf_counter() - self._start) * 1000, 1),
        }
        if self._first_token is not None:
            record["ttft_ms"] = round((self._first_token - self._start) * 1000, 1)
        if usage is not None:
            record["prompt_tokens"] = usage.prompt_tokens
            record["completion_tokens"] = usage.completion_tokens
        if error is not None:
            record["error"] = error
        access_logger.info(json.dumps(record, separators=(",", ":")))
