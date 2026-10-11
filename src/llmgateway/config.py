"""Gateway configuration: which providers exist and which model aliases route to them.

The config is loaded once at startup from YAML and validated up front, so a typo in a
provider name fails the deploy instead of failing the first request that hits that route.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class TimeoutConfig(BaseModel):
    connect_s: float = Field(2.0, gt=0, description="TCP/TLS connect budget.")
    read_s: float = Field(
        30.0,
        gt=0,
        description="Max silence between bytes. For streams this bounds the gap between tokens.",
    )
    total_s: float = Field(
        120.0, gt=0, description="Wall-clock deadline for a whole non-streaming request."
    )


class ProviderConfig(BaseModel):
    name: str
    type: Literal["openai", "azure_openai"]
    base_url: str
    api_key_env: str | None = Field(
        None, description="Name of the env var holding the key. Keys never live in the YAML."
    )
    api_version: str | None = Field(None, description="Required for azure_openai.")
    stream_usage: bool = Field(
        True,
        description="Ask the upstream to append a usage chunk to streams (stream_options).",
    )
    timeout: TimeoutConfig = TimeoutConfig()

    @model_validator(mode="after")
    def _azure_needs_version(self) -> ProviderConfig:
        if self.type == "azure_openai" and not self.api_version:
            raise ValueError(f"provider {self.name!r}: azure_openai requires api_version")
        return self

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class RouteTarget(BaseModel):
    provider: str
    model: str = Field(description="Model name (or Azure deployment name) at the provider.")


class ModelRoute(BaseModel):
    alias: str = Field(description="The model name clients send to the gateway.")
    targets: list[RouteTarget] = Field(min_length=1)


class GatewayConfig(BaseModel):
    providers: list[ProviderConfig] = Field(min_length=1)
    models: list[ModelRoute] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        aliases = [m.alias for m in self.models]
        if len(aliases) != len(set(aliases)):
            raise ValueError("model aliases must be unique")
        known = set(names)
        for route in self.models:
            for target in route.targets:
                if target.provider not in known:
                    raise ValueError(
                        f"model {route.alias!r} routes to unknown provider {target.provider!r}"
                    )
        return self


def load_config(path: str | Path) -> GatewayConfig:
    with open(path, encoding="utf-8") as f:
        return GatewayConfig.model_validate(yaml.safe_load(f))
