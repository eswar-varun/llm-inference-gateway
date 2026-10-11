from __future__ import annotations

import httpx

from ..config import GatewayConfig
from .base import Provider
from .openai_compat import OpenAICompatibleProvider

__all__ = ["OpenAICompatibleProvider", "Provider", "build_providers"]


def build_providers(config: GatewayConfig, client: httpx.AsyncClient) -> dict[str, Provider]:
    # Both supported types speak the same wire format; Bedrock (SigV4, Converse API) will
    # need its own class.
    return {p.name: OpenAICompatibleProvider(p, client) for p in config.providers}
