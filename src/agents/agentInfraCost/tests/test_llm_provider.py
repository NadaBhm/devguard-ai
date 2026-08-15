"""Tests for core.llm_provider — NEVER a real network call.

httpx.post is always monkeypatched; no test here can reach OpenRouter.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from core.llm_provider import _DEFAULT_MODEL, call_llm


class _FakeResponse:
    def __init__(
        self, *, status_code: int = 200, payload: dict[str, Any] | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

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

    assert captured["model"] == _DEFAULT_MODEL


def test_call_llm_honors_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["model"] = json["model"]
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s", model="anthropic/claude-3.5-sonnet")

    assert captured["model"] == "anthropic/claude-3.5-sonnet"


def test_call_llm_sends_provider_order_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["provider"] = json.get("provider")
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s", provider_order=["nvidia"])

    assert captured["provider"] == {"order": ["nvidia"], "allow_fallbacks": False}


def test_call_llm_omits_provider_field_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["has_provider_key"] = "provider" in json
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s")

    assert captured["has_provider_key"] is False


def test_call_llm_reads_provider_order_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_PROVIDER", "nvidia, deepseek")
    captured = {}

    def _fake_post(url, headers, json, timeout):
        captured["provider"] = json.get("provider")
        return _FakeResponse(payload={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    call_llm(prompt="p", system_instruction="s")

    assert captured["provider"] == {"order": ["nvidia", "deepseek"], "allow_fallbacks": False}


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
    """A permanent 4xx must fail immediately — no retries, still None."""
    def _fake_post(url, headers, json, timeout):
        return _FakeResponse(status_code=400)

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
    """Even under retries, a persistently malformed body resolves to None."""
    def _fake_post(url, headers, json, timeout):
        return _FakeResponse(payload={"unexpected": "shape"})

    monkeypatch.setattr("core.llm_provider.httpx.post", _fake_post)
    # Force max-retries to 1 so the exhausted-fallback path is exercised fast,
    # not slowed down by the backoff sleeps.
    monkeypatch.setattr("core.llm_provider._MAX_LLM_RETRIES", 1)
    assert call_llm(prompt="p", system_instruction="s") is None


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


@pytest.fixture
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real sleeps have no place in the test suite."""
    monkeypatch.setattr("core.llm_provider.time.sleep", lambda _seconds: None)


def _counting_fake(responses, *, n_expected: int | None = None):
    calls = {"n": 0}

    def _fake_post(url, headers, json, timeout):
        calls["n"] += 1
        resp = responses[min(calls["n"] - 1, len(responses) - 1)]
        if isinstance(resp, BaseException):
            raise resp
        return resp

    _fake_post._calls = calls
    _fake_post._expected = n_expected or len(responses)
    return _fake_post


def _assert_called_times(fake, expected: int) -> None:
    assert fake._calls["n"] == expected


def test_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep) -> None:
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "recovered"}}]})
    fake = _counting_fake([_FakeResponse(status_code=503), ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "recovered"
    _assert_called_times(fake, 2)


def test_retries_provider_404_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    """OpenRouter's 'Provider returned error' 404 is an upstream hiccup, and
    the very failure we hit during the live Gate-2 loop."""
    flaky = _FakeResponse(
        status_code=404,
        text=json.dumps(
            {
                "error": {
                    "message": "Provider returned error",
                    "code": 404,
                    "metadata": {"provider_name": "Nvidia"},
                }
            }
        ),
    )
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "pong"}}]})
    fake = _counting_fake([flaky, ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "pong"
    _assert_called_times(fake, 2)


def test_does_not_retry_plain_404_unknown_model(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    """A 404 from OpenRouter itself (unknown model slug) is permanent."""
    missing = _FakeResponse(
        status_code=404,
        text=json.dumps({"error": {"message": "Model not found", "code": 404}}),
    )
    fake = _counting_fake([missing])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") is None
    _assert_called_times(fake, 1)


def test_retries_connection_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "hello"}}]})
    fake = _counting_fake([httpx.RemoteProtocolError("incomplete chunked read"), ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "hello"
    _assert_called_times(fake, 2)


def test_retries_malformed_200_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "answer"}}]})
    glitch = _FakeResponse(payload={"unexpected": "shape"})
    fake = _counting_fake([glitch, ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "answer"
    _assert_called_times(fake, 2)


def test_retries_wrapped_502_in_200_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    """OpenRouter wraps upstream failures (e.g. Nvidia 502) in an HTTP 200
    with an error body instead of a completion. That must be treated like a
    5xx — retried with backoff — not silently swallowed as a permanent
    parse failure (the bug that wedged a live job in a retry loop)."""
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "answer"}}]})
    glitch = _FakeResponse(
        payload={
            "error": {
                "message": "Upstream error from Nvidia: Internal server error",
                "code": 502,
            }
        }
    )
    fake = _counting_fake([glitch, ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "answer"
    _assert_called_times(fake, 2)


def test_wrapped_error_in_200_is_retryable_not_parse_failure(
    monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep
) -> None:
    ok = _FakeResponse(payload={"choices": [{"message": {"content": "answer"}}]})
    glitch = _FakeResponse(payload={"error": {"message": "boom", "code": 502}})
    fake = _counting_fake([glitch, ok])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") == "answer"
    _assert_called_times(fake, 2)


def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep) -> None:
    fake = _counting_fake([_FakeResponse(status_code=503)])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") is None
    _assert_called_times(fake, 3)


def test_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch, _no_backoff_sleep) -> None:
    """A timeout already cost the caller the full budget; repeating it with
    backoff would only compound latency."""
    fake = _counting_fake([httpx.ReadTimeout("slow model")])
    monkeypatch.setattr("core.llm_provider.httpx.post", fake)
    assert call_llm(prompt="p", system_instruction="s") is None
    _assert_called_times(fake, 1)
