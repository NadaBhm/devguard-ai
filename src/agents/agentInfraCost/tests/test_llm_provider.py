"""Tests for core.llm_provider — NEVER a real network call.

httpx.post is always monkeypatched; no test here can reach OpenRouter.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.llm_provider import call_llm


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _has_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-real-key")


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_call_llm_returns_message_content_from_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_post(url, headers, json, timeout):
        assert url.startswith("https://openrouter.ai/")
        assert headers["Authorization"] == "Bearer dummy-not-a-real-key"
        assert json["messages"][1]["content"] == "hello"
        return _FakeResponse(payload={"choices": [{"message": {"content": "a real answer"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)

    assert call_llm(prompt="hello", system_instruction="be terse") == "a real answer"


def test_call_llm_uses_default_model_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["model"] = json["model"]
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s")

    assert captured["model"] == "openai/gpt-4o-mini"


def test_call_llm_honors_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["model"] = json["model"]
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s", model="anthropic/claude-3.5-sonnet")

    assert captured["model"] == "anthropic/claude-3.5-sonnet"


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_call_llm_returns_none_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert call_llm(prompt="p", system_instruction="s") is None


def test_call_llm_env_model_used_when_no_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", "mistralai/mistral-large")
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["model"] = json["model"]
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s")

    assert captured["model"] == "mistralai/mistral-large"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_call_llm_returns_none_on_non_2xx_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_post(url, headers, json, timeout):
        return _FakeResponse(status_code=500)

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    assert call_llm(prompt="p", system_instruction="s") is None


def test_call_llm_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_post(url, headers, json, timeout):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    assert call_llm(prompt="p", system_instruction="s") is None


def test_call_llm_returns_none_on_malformed_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_post(url, headers, json, timeout):
        return _FakeResponse(payload={"unexpected": "shape"})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    assert call_llm(prompt="p", system_instruction="s") is None
