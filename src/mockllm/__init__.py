"""Fake OpenAI-compatible LLM server for tests, load tests and fault injection."""

from .app import MockConfig, create_mock_app

__all__ = ["MockConfig", "create_mock_app"]
