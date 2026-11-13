"""P5-03: typed speech output and playback acknowledgment contracts.

The host authenticates playback callbacks; tokens are correlations, not
credentials. Audio status is not evidence that a goal or tool completed.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SpeechClipStatus = Literal[
    "GENERATED", "QUEUED", "STARTED", "PLAYED",
    "INTERRUPTED", "FAILED", "TIMEOUT",
]
PlaybackStatus = Literal[
    "QUEUED", "STARTED", "PLAYED", "INTERRUPTED", "FAILED",
]


class SpeechOutputTarget(BaseModel):
    """Server-side authorized output channel binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    channel_id: str = Field(min_length=1, max_length=128)


class SpeechClipCommand(BaseModel):
    """Audio metadata only. NEVER put waveform bytes in this model or events."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    response_id: UUID
    clip_index: int = Field(ge=0, lt=64)
    clip_count: int = Field(ge=1, le=64)
    channel_id: str = Field(min_length=1, max_length=128)
    sample_rate_hz: Literal[16000, 22050, 24000, 48000]
    duration_ms: int = Field(ge=1, le=30000)
    payload_sha256: str = Field(min_length=64, max_length=64)


class PlaybackAck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    response_id: UUID
    clip_id: UUID
    channel_id: str = Field(min_length=1, max_length=128)
    playback_token: UUID
    status: PlaybackStatus


class SpeechClipResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    response_id: UUID
    clip_id: UUID
    clip_index: int
    clip_count: int
    channel_id: str
    playback_token: UUID
    status: SpeechClipStatus
    response_delivered: bool = False
    replayed: bool = False


@dataclass(frozen=True)
class GeneratedSpeechClip:
    """Ephemeral payload returned for authorized host delivery; not persisted."""

    record: SpeechClipResult
    audio_wav: bytes = field(repr=False)


class SpeechSynthesizer(Protocol):
    """Synthesize one bounded text segment without a DB transaction."""

    def synthesize_wav(self, text: str) -> bytes:
        """Return complete PCM16 mono WAV for this clip, no side effects."""


class InvalidSpeechAudio(ValueError):
    pass


class SpeechDeliveryConflict(Exception):
    pass
