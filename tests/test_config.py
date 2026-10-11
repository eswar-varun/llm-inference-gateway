from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from llmgateway.config import GatewayConfig, load_config

EXAMPLE = Path(__file__).parent.parent / "config" / "gateway.example.yaml"


def _config(providers, models):
    return GatewayConfig.model_validate({"providers": providers, "models": models})


_MOCK = {"name": "mock", "type": "openai", "base_url": "http://x"}


def test_example_config_is_valid():
    config = load_config(EXAMPLE)
    assert {m.alias for m in config.models} >= {"mock-small", "gpt-4o-mini"}


def test_route_to_unknown_provider_is_rejected_at_load_time():
    with pytest.raises(ValidationError, match="unknown provider 'typo'"):
        _config([_MOCK], [{"alias": "m", "targets": [{"provider": "typo", "model": "x"}]}])


def test_duplicate_provider_names_are_rejected():
    with pytest.raises(ValidationError, match="provider names must be unique"):
        _config([_MOCK, _MOCK], [{"alias": "m", "targets": [{"provider": "mock", "model": "x"}]}])


def test_duplicate_aliases_are_rejected():
    route = {"alias": "m", "targets": [{"provider": "mock", "model": "x"}]}
    with pytest.raises(ValidationError, match="model aliases must be unique"):
        _config([_MOCK], [route, route])


def test_azure_requires_api_version():
    azure = {"name": "az", "type": "azure_openai", "base_url": "https://x"}
    with pytest.raises(ValidationError, match="requires api_version"):
        _config([azure], [{"alias": "m", "targets": [{"provider": "az", "model": "x"}]}])


def test_route_needs_at_least_one_target():
    with pytest.raises(ValidationError):
        _config([_MOCK], [{"alias": "m", "targets": []}])
