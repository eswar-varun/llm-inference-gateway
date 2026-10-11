from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from ..config import ProviderConfig


class Provider(ABC):
    """An upstream that speaks (some dialect of) the Chat Completions API.

    Implementations translate transport failures into GatewayError so callers above this
    layer never see httpx exceptions.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a non-streaming request and return the parsed response body."""

    @abstractmethod
    def stream(self, payload: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
        """Send a streaming request and yield parsed chunks until the upstream finishes.

        Callers must close the generator (e.g. `contextlib.aclosing`) so the upstream
        connection is released even when the client stops reading early.
        """
