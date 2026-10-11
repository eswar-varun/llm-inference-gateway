"""The slice of the OpenAI Chat Completions schema the gateway needs to understand.

Models allow extra fields: the gateway validates what it routes on and forwards everything
else untouched, so new upstream parameters (tools, response_format, seed, ...) work without a
gateway release.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Usage | None:
        raw = body.get("usage")
        return cls.model_validate(raw) if isinstance(raw, dict) else None


def error_body(message: str, type_: str, code: str | None = None, param: str | None = None):
    """OpenAI-style error envelope, so existing SDKs surface gateway errors correctly."""
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}
