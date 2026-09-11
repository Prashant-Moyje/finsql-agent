"""Groq client rate-limit handling, with the HTTP call mocked (no network)."""
import time

import httpx
import pytest

from finsql import llm


def _resp(status: int, retry_after: str | None = None, content: str = "SELECT 1") -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after else {}
    body = {"choices": [{"message": {"content": content}}]} if status == 200 else {"error": "rate_limited"}
    return httpx.Response(status, json=body, headers=headers,
                          request=httpx.Request("POST", llm.GroqLLM.URL))


def test_short_rate_limit_is_retried(monkeypatch):
    responses = [_resp(429, "0.01"), _resp(200)]
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: responses.pop(0))
    assert llm.GroqLLM(api_key="test").complete("sys", "user") == "SELECT 1"


def test_long_retry_after_fails_fast_instead_of_hanging(monkeypatch):
    # A daily-quota retry-after can be hours; the eval once sat on one for over an hour.
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _resp(429, "3600"))
    t0 = time.perf_counter()
    with pytest.raises(RuntimeError, match="rate limit reached"):
        llm.GroqLLM(api_key="test").complete("sys", "user")
    assert time.perf_counter() - t0 < 2


def test_http_errors_include_response_body(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _resp(404))
    with pytest.raises(RuntimeError, match="Groq error 404"):
        llm.GroqLLM(api_key="test").complete("sys", "user")
