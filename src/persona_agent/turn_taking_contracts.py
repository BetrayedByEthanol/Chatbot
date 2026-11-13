"""P5-04 barge-in, echo policy, and scoped durable speech-stop contracts."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .streaming_asr import DecodeRequest


class SpeechInterruptCommand(BaseModel):
    """Trusted host constructs this after stopping audible output immediately."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    response_id: UUID
    conversation_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    output_channel_id: str = Field(min_length=1,max_length=128)
    input_channel_id: str = Field(min_length=1,max_length=128)
    reason: Literal["USER_SPEECH","CLIENT_STOP"]
    utterance_id: UUID | None = None

    @model_validator(mode="after")
    def require_user_utterance(self):
        if (self.reason == "USER_SPEECH") != (self.utterance_id is not None):
            raise ValueError("user-speech interruption requires utterance provenance")
        return self


class SpeechInterruptResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interruption_id: UUID
    response_id: UUID
    interrupted_clips: int = Field(ge=0,le=64)
    replayed: bool = False


@dataclass(frozen=True)
class TurnFrameResult:
    """Pure in-memory perception decision, never a conversation turn."""

    suppressed_echo_risk: bool = False
    speech_started: bool = False
    interrupt_requested: bool = False
    decode_request: DecodeRequest | None = None
