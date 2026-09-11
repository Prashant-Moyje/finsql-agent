"""Minimal chat client for Ollama (local) and Groq (hosted free tier).

Plain HTTP instead of LangChain wrappers: fewer dependencies in the Lambda zip,
and full control over details like switching off Qwen3's thinking mode."""
import re
import time

import httpx

from . import config

_THINK = re.compile(r"<think>.*?</think>", re.S)
MAX_RATE_LIMIT_WAIT_S = 30  # longer waits mean a quota is exhausted: fail fast


class LLM:
    name = "base"

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OllamaLLM(LLM):
    def __init__(self, model: str = config.OLLAMA_MODEL, url: str = config.OLLAMA_URL):
        self.model, self.url = model, url.rstrip("/")
        self.name = f"ollama:{model}"

    def complete(self, system: str, user: str) -> str:
        r = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_ctx": 8192},
            },
            timeout=300,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return _THINK.sub("", r.json()["message"]["content"]).strip()


class GroqLLM(LLM):
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str = config.GROQ_MODEL, api_key: str = config.GROQ_API_KEY):
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=groq but GROQ_API_KEY is not set")
        self.model, self.api_key = model, api_key
        self.name = f"groq:{model}"

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        for attempt in range(4):
            r = httpx.post(self.URL, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=60)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", 2 ** attempt))
                # Per-minute limits clear in seconds: wait and retry. A daily-quota
                # retry-after can be hours: fail fast instead of hanging (a Slack
                # user would otherwise wait forever).
                if wait <= MAX_RATE_LIMIT_WAIT_S and attempt < 3:
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Groq free-tier rate limit reached (retry after ~{wait / 60:.0f} min): {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"Groq error {r.status_code} (model {self.model}): {r.text[:300]}")
            return _THINK.sub("", r.json()["choices"][0]["message"]["content"]).strip()
        raise RuntimeError("Groq rate limit: retries exhausted")


def get_llm() -> LLM:
    return GroqLLM() if config.LLM_PROVIDER == "groq" else OllamaLLM()
