from __future__ import annotations

import re

from helpers import ALIAS, UPSTREAM_MODEL, chat_body, make_config


async def test_completion_is_routed_and_returned(gateway, mock_llm):
    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "the quick brown fox jumps"
    assert body["usage"] == {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7}
    assert response.headers["x-gateway-provider"] == "mock"
    assert response.headers["x-gateway-upstream-model"] == UPSTREAM_MODEL

    sent = mock_llm.state.requests[-1]["body"]
    assert sent["model"] == UPSTREAM_MODEL  # alias rewritten to the provider's model name
    assert sent["stream"] is False


async def test_unknown_parameters_are_forwarded_untouched(gateway, mock_llm):
    extras = {"temperature": 0.2, "top_p": 0.9, "seed": 42,
              "response_format": {"type": "json_object"}, "user": None}
    await gateway.post("/v1/chat/completions", json=chat_body(**extras))

    sent = mock_llm.state.requests[-1]["body"]
    for key, value in extras.items():
        assert sent[key] == value
    assert "stream_options" not in sent


async def test_request_id_is_echoed_or_generated(gateway):
    echoed = await gateway.post("/v1/chat/completions", json=chat_body(),
                                headers={"X-Request-ID": "trace-abc_123"})
    assert echoed.headers["x-request-id"] == "trace-abc_123"

    # Unsafe values (spaces, newlines -> header/log injection) are replaced, not trusted.
    replaced = await gateway.post("/v1/chat/completions", json=chat_body(),
                                  headers={"X-Request-ID": "bad id"})
    assert re.fullmatch(r"[0-9a-f]{32}", replaced.headers["x-request-id"])


async def test_unknown_model_is_an_openai_style_404(gateway):
    response = await gateway.post("/v1/chat/completions", json=chat_body(model="nope"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_invalid_body_is_an_openai_style_400(gateway):
    response = await gateway.post("/v1/chat/completions", json={"model": ALIAS})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "messages"


async def test_malformed_json_is_a_400(gateway):
    response = await gateway.post("/v1/chat/completions", content=b"{not json",
                                  headers={"content-type": "application/json"})
    assert response.status_code == 400


async def test_models_endpoint_lists_aliases(gateway):
    response = await gateway.get("/v1/models")
    assert [m["id"] for m in response.json()["data"]] == [ALIAS]


async def test_healthz(gateway):
    assert (await gateway.get("/healthz")).json() == {"status": "ok"}


async def test_azure_provider_uses_deployment_path_and_api_key(make_gateway, mock_llm,
                                                               monkeypatch):
    monkeypatch.setenv("TEST_AZURE_KEY", "secret-key")
    config = make_config(type="azure_openai", base_url="http://upstream",
                         api_version="2024-10-21", api_key_env="TEST_AZURE_KEY")
    gateway = await make_gateway(config)

    response = await gateway.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 200
    sent = mock_llm.state.requests[-1]
    assert sent["path"] == f"/openai/deployments/{UPSTREAM_MODEL}/chat/completions"
    assert sent["query"] == "api-version=2024-10-21"
    assert sent["headers"]["api-key"] == "secret-key"
    assert "authorization" not in sent["headers"]


async def test_openai_provider_sends_bearer_key(make_gateway, mock_llm, monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    gateway = await make_gateway(make_config(api_key_env="TEST_OPENAI_KEY"))

    await gateway.post("/v1/chat/completions", json=chat_body())

    assert mock_llm.state.requests[-1]["headers"]["authorization"] == "Bearer sk-test"
