"""Dependency-free Ollama text adapter, not the legacy singleton model manager.

Only the configured model endpoint is called. Model inference happens outside
database locks; callers may substitute an in-process deterministic fake.
"""

import json
import socket
from dataclasses import dataclass
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .conversation_contracts import ModelMessage, ModelReply, ModelUnavailable


@dataclass(frozen=True)
class OllamaChatGateway:
    model: str
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = 25.0
    max_response_chars: int = 12000

    def __post_init__(self) -> None:
        if not (1 <= len(self.model) <= 128):
            raise ValueError("model identifier must contain 1–128 characters")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1 <= self.max_response_chars <= 12000:
            raise ValueError("max_response_chars must be between 1 and 12000")
        if not (self.base_url.startswith("http://") or
                self.base_url.startswith("https://")):
            raise ValueError("model endpoint must be HTTP(S)")

    def generate(self, messages: Sequence[ModelMessage]) -> ModelReply:
        if not messages:
            raise ValueError("model messages cannot be empty")
        request_body = json.dumps({
            "model": self.model,
            "stream": False,
            "messages": [{"role": message.role, "content": message.content}
                         for message in messages],
        }).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + "/api/chat",
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(256_001)
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ModelUnavailable("model gateway request failed") from exc
        if len(payload) > 256_000:
            raise ModelUnavailable("model gateway response exceeds size limit")
        try:
            document = json.loads(payload)
            text = document["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("model content is not text")
            text = text.strip()
            if not text or len(text) > self.max_response_chars:
                raise ValueError("model returned empty or oversized text")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ModelUnavailable("model gateway returned invalid response") from exc
        return ModelReply(model_id=self.model, text=text)
