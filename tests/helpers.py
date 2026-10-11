from __future__ import annotations

import json
from typing import Any

from llmgateway.config import GatewayConfig

ALIAS = "test-model"
UPSTREAM_MODEL = "mock-1"


def make_config(base_url: str = "http://upstream/v1", **provider: Any) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "providers": [{"name": "mock", "type": "openai", "base_url": base_url, **provider}],
        "models": [{"alias": ALIAS, "targets": [{"provider": "mock", "model": UPSTREAM_MODEL}]}],
    })


def chat_body(**overrides: Any) -> dict[str, Any]:
    return {"model": ALIAS, "messages": [{"role": "user", "content": "hello there"}],
            **overrides}


def sse_events(text: str) -> list[Any]:
    """Parse a gateway SSE body into decoded JSON events, keeping the '[DONE]' sentinel."""
    events = []
    for block in text.strip().split("\n\n"):
        assert block.startswith("data: "), block
        data = block[len("data: "):]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events
