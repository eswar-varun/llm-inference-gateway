"""One error type for everything that can go wrong on the request path.

Each failure is classified by *kind*, and the kind alone decides the HTTP status the client
sees and whether the attempt is safe to retry. Keeping that table in one place is what lets
later milestones (retries, failover, circuit breaking) make decisions without string-matching
error messages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .schemas import error_body


class ErrorKind(StrEnum):
    INVALID_REQUEST = "invalid_request"  # the client's request is bad; retrying won't help
    MODEL_NOT_FOUND = "model_not_found"  # no route for the requested alias
    RATE_LIMITED = "rate_limited"  # upstream (or, later, the gateway) throttled us
    TIMEOUT = "timeout"  # upstream too slow
    CONNECTION = "connection"  # could not reach upstream, or it dropped the connection
    UPSTREAM_SERVER = "upstream_server"  # upstream 5xx
    UPSTREAM_CONFIG = "upstream_config"  # upstream rejected *our* key/deployment: gateway misconfig
    UPSTREAM_PROTOCOL = "upstream_protocol"  # upstream sent something we can't parse
    OVERLOADED = "overloaded"  # the gateway itself is out of capacity
    INTERNAL = "internal"


_HTTP_STATUS: dict[ErrorKind, int] = {
    ErrorKind.INVALID_REQUEST: 400,
    ErrorKind.MODEL_NOT_FOUND: 404,
    ErrorKind.RATE_LIMITED: 429,
    ErrorKind.TIMEOUT: 504,
    ErrorKind.CONNECTION: 502,
    ErrorKind.UPSTREAM_SERVER: 502,
    ErrorKind.UPSTREAM_CONFIG: 502,
    ErrorKind.UPSTREAM_PROTOCOL: 502,
    ErrorKind.OVERLOADED: 503,
    ErrorKind.INTERNAL: 500,
}

_OPENAI_TYPE: dict[ErrorKind, str] = {
    ErrorKind.INVALID_REQUEST: "invalid_request_error",
    ErrorKind.MODEL_NOT_FOUND: "invalid_request_error",
    ErrorKind.RATE_LIMITED: "rate_limit_error",
}


class GatewayError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        retryable: bool = False,
        provider: str | None = None,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.provider = provider
        self.retry_after_s = retry_after_s
        # Lets an upstream 413/422 reach the client as-is instead of being flattened to 400.
        self._status_code = status_code

    @property
    def http_status(self) -> int:
        return self._status_code or _HTTP_STATUS[self.kind]

    def body(self) -> dict[str, Any]:
        return error_body(self.message, _OPENAI_TYPE.get(self.kind, "api_error"), self.kind.value)

    def __repr__(self) -> str:
        return f"GatewayError({self.kind}, {self.message!r}, provider={self.provider!r})"


def classify_status(status: int) -> tuple[ErrorKind, bool]:
    """Map an upstream HTTP status to (kind, retryable-on-the-same-provider)."""
    if status == 429:
        return ErrorKind.RATE_LIMITED, True
    if status == 408:
        return ErrorKind.TIMEOUT, True
    if status in (400, 413, 422):
        return ErrorKind.INVALID_REQUEST, False
    if status in (401, 403, 404):
        return ErrorKind.UPSTREAM_CONFIG, False
    if status >= 500:
        return ErrorKind.UPSTREAM_SERVER, status != 501
    return ErrorKind.UPSTREAM_SERVER, False
