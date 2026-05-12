"""
openai_shim.py
--------------
Minimal OpenAI client using only Python stdlib (urllib + json).
Implements the subset of the openai package used by msds_translation_engine.py:
  - client.chat.completions.create(...)   → returns object with .choices[0].message.content
  - client.responses.create(...)          → returns object with .output_text
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


class _SimpleNamespace:
    """Dot-accessible dict wrapper."""
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Exception shims (used by msds_translation_engine for retry logic)
# ---------------------------------------------------------------------------

class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class RateLimitError(Exception):
    pass


# ---------------------------------------------------------------------------
# Core HTTP helper
# ---------------------------------------------------------------------------

BASE_URL = "https://api.openai.com/v1"
_ctx = ssl.create_default_context()


def _post(url: str, payload: dict, api_key: str, timeout: float = 180) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 429:
            raise RateLimitError(body) from exc
        if exc.code in (500, 502, 503, 504):
            raise APIConnectionError(body) from exc
        raise
    except (TimeoutError, OSError) as exc:
        raise APITimeoutError(str(exc)) from exc


# ---------------------------------------------------------------------------
# chat.completions.create
# ---------------------------------------------------------------------------

class _CompletionsEndpoint:
    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def create(
        self,
        model: str,
        messages: list[dict],
        max_completion_tokens: int = 4096,
        timeout: float = 180,
        response_format: dict | None = None,
        **_kwargs: Any,
    ) -> Any:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        resp = _post(f"{BASE_URL}/chat/completions", payload, self._key, timeout=timeout)
        content = resp["choices"][0]["message"]["content"]
        return _SimpleNamespace(
            choices=[
                _SimpleNamespace(
                    message=_SimpleNamespace(content=content)
                )
            ]
        )


class _ChatEndpoint:
    def __init__(self, api_key: str) -> None:
        self.completions = _CompletionsEndpoint(api_key)


# ---------------------------------------------------------------------------
# responses.create  (maps to chat/completions with system+user pattern)
# ---------------------------------------------------------------------------

class _ResponsesEndpoint:
    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def create(
        self,
        model: str,
        instructions: str = "",
        input: str = "",  # noqa: A002
        max_output_tokens: int = 1200,
        timeout: float = 120,
        **_kwargs: Any,
    ) -> Any:
        messages: list[dict] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": input})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_output_tokens,
        }
        resp = _post(f"{BASE_URL}/chat/completions", payload, self._key, timeout=timeout)
        text = resp["choices"][0]["message"]["content"] or ""
        return _SimpleNamespace(output_text=text.strip())


# ---------------------------------------------------------------------------
# Public OpenAI class
# ---------------------------------------------------------------------------

class OpenAI:
    def __init__(self, api_key: str | None = None) -> None:
        if not api_key:
            import os
            api_key = os.environ.get("OPENAI_API_KEY", "")
        self._key = api_key
        self.chat = _ChatEndpoint(api_key)
        self.responses = _ResponsesEndpoint(api_key)
