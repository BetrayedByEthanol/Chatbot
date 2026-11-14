"""Dependency-free Ollama text adapter, not the legacy singleton model manager.

Only the configured model endpoint is called. Model inference happens outside
database locks; callers may substitute an in-process deterministic fake.
"""

import json
import socket
from dataclasses import dataclass
from typing import Sequence

from pydantic import ValidationError
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .conversation_contracts import (
    ModelIntentCandidate, ModelMessage, ModelReply, ModelUnavailable,
)


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

    def _chat(self, messages: Sequence[ModelMessage], *, json_mode: bool = False) -> str:
        if not messages:
            raise ValueError("model messages cannot be empty")
        request_payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
        }
        if json_mode:
            request_payload["format"] = "json"
        request = Request(
            self.base_url.rstrip("/") + "/api/chat",
            data=json.dumps(request_payload).encode("utf-8"),
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
            # Structured envelope may exceed the final reply length.
            limit = self.max_response_chars + 2048 if json_mode else self.max_response_chars
            if not text or len(text) > limit:
                raise ValueError("model returned empty or oversized text")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ModelUnavailable("model gateway returned invalid response") from exc
        return text

    def generate(self, messages: Sequence[ModelMessage]) -> ModelReply:
        return ModelReply(model_id=self.model, text=self._chat(messages))

    def generate_interpreted(
        self, messages: Sequence[ModelMessage],
    ) -> ModelReply:
        """One primary model call produces both spoken reply and optional cue.

        This is deliberately an alternate single-call route, not a second
        interpretation request. Malformed candidate metadata is discarded.
        The caller independently validates source evidence and transaction
        freshness before persisting anything.
        """
        if not messages:
            raise ValueError("model messages cannot be empty")
        instruction = (
            "Answer the user's latest message naturally and briefly, in the "
            "same language. Output ONLY a JSON object with keys 'reply' "
            "(ordinary response string) and 'intent' (object or null). "
            "If a meaningful tentative user intent is supported by the "
            "LATEST user message, intent has exactly keys: 'kind' "
            "(OBJECTIVE, REQUEST, OBSERVATION, CORRECTION, HYPOTHETICAL, "
            "or INSTRUCTION), 'evidence_quote' (an exact substring copied "
            "from the latest user message, length 3..200), 'confidence' "
            "(number 0..1), and 'reference' (NONE or PRIOR_OBJECTIVE). "
            "Set 'reference' to 'PRIOR_OBJECTIVE' only if the user explicitly "
            "refers back to one unique earlier objective in this conversation; "
            "otherwise set 'reference' to the string 'NONE' (never null). "
            "Set 'intent' to null ONLY when the latest user message contains "
            "no supported intent. Do not invent facts, authority, tool "
            "results, goal approvals, or completion evidence. Never include "
            "metadata outside the JSON object."
        )
        structured = (
            ModelMessage("system", instruction),
            *messages,
        )
        raw = self._chat(structured, json_mode=True)
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            # If a model ignores JSON mode, do not surface JSON-like garbage,
            # and never make a second remote inference as a fallback.
            if raw.startswith(("{", "[")):
                raise ModelUnavailable("invalid structured model reply")
            if len(raw) > self.max_response_chars:
                raise ModelUnavailable("oversized plain model fallback")
            return ModelReply(model_id=self.model, text=raw)
        if isinstance(envelope, str):
            # Some small models produce a JSON string rather than an object.
            # It is still a safe conversational reply, without metadata.
            if not envelope.strip() or len(envelope.strip()) > self.max_response_chars:
                raise ModelUnavailable("structured model reply has no response text")
            return ModelReply(model_id=self.model, text=envelope.strip())
        if not isinstance(envelope, dict):
            raise ModelUnavailable("structured model reply must be an object")
        reply = envelope.get("reply")
        if not isinstance(reply, str):
            # Tolerate a valid plain reply under a common alternate field;
            # never render the entire JSON envelope to the user.
            reply = next(
                (envelope[key] for key in ("response", "answer")
                 if isinstance(envelope.get(key), str)),
                None,
            )
        if not isinstance(reply, str) or not reply.strip():
            raise ModelUnavailable("structured model reply has no response text")
        reply = reply.strip()
        if len(reply) > self.max_response_chars:
            raise ModelUnavailable("structured model reply is oversized")
        candidate = None
        if isinstance(envelope.get("intent"), dict):
            try:
                candidate = ModelIntentCandidate.model_validate(envelope["intent"])
            except ValidationError:
                pass  # Never fail or retry a valid reply for optional metadata.
        return ModelReply(
            model_id=self.model, text=reply, intent_candidate=candidate,
        )
