"""P5-01 transport-independent speech ingress contracts.

Speech providers emit untrusted transcript hypotheses, never authenticated
actors, selection generations, or user instructions. The hosting transport
constructs VoiceInputSource only after authenticating the user and binding
the channel to the selected conversation.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class VoiceInputSource(BaseModel):
    """Trusted host-supplied channel binding; never populated from ASR text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    channel_id: str = Field(min_length=1, max_length=128)


class TranscriptUpdate(BaseModel):
    """An ASR observation; only FINAL is eligible for conversation admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance_id: UUID
    revision: int = Field(ge=0)
    status: Literal["PARTIAL", "FINAL"]
    text: str = Field(max_length=12000)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def require_aware_capture_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def require_final_text(self):
        if self.status == "FINAL" and not self.text.strip():
            raise ValueError("final transcript must contain text")
        return self


class VoiceInputProvenance(BaseModel):
    """Non-authoritative observation metadata attached to a committed turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance_id: UUID
    channel_id: str = Field(min_length=1, max_length=128)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def require_aware_capture_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture timestamp must include a timezone offset")
        return value


class TranscriptIngressResult(BaseModel):
    """Provisional transcripts have no durable turn/event identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance_id: UUID
    disposition: Literal["PROVISIONAL", "COMMITTED"]
    turn_id: UUID | None = None
    event_id: UUID | None = None
    conversation_sequence: int | None = None
    replayed: bool = False
