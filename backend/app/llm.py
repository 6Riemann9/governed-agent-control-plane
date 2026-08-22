"""Bounded OpenAI-compatible completion client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    """A safe-to-persist provider failure without request credentials or bodies."""


@dataclass(frozen=True)
class Completion:
    content: str
    input_tokens: int
    output_tokens: int


class OpenAICompatibleExecutor:
    def __init__(self, base_url: str, api_key: str, model: str, max_tokens: int, timeout_seconds: int) -> None:
        base = base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self.url = f"{base}/chat/completions"
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        task: str,
        node_name: str,
        prior_output: str,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        context = prior_output[-4_000:] if prior_output else "(no prior node output)"
        payload = {
            "model": model or self.model,
            "temperature": 0.2,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a governed agent worker. Return a concise, factual result for your assigned DAG node.",
                },
                {
                    "role": "user",
                    "content": f"Task:\n{task}\n\nCurrent node: {node_name}\n\nPrior node output:\n{context}",
                },
            ],
        }
        request = Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - configured HTTPS endpoint
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise LLMError(f"provider returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise LLMError("provider connection failed") from error
        except json.JSONDecodeError as error:
            raise LLMError("provider returned invalid JSON") from error

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError("provider response contained no assistant content") from error
        if not isinstance(content, str) or not content.strip():
            raise LLMError("provider returned empty assistant content")
        usage: dict[str, Any] = body.get("usage") or {}
        return Completion(
            content=content.strip(),
            input_tokens=_non_negative_int(usage.get("prompt_tokens")),
            output_tokens=_non_negative_int(usage.get("completion_tokens")),
        )


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
