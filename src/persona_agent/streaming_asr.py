"""P5-02 bounded PCM16 speech-stream assembly and ASR result correlation.

This is transient, per-utterance state, NOT another conversation manager or
scheduler. Frame admission never calls a model or accesses PostgreSQL.
The host executes each returned DecodeRequest using an approved ASR provider
outside database transactions, then applies the result.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isqrt
from sys import byteorder
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID
from array import array

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg import Connection

from .contracts import TrustedActor
from .multimodal_contracts import TranscriptIngressResult, TranscriptUpdate, VoiceInputSource
from .multimodal_ingress import ingest_voice_transcript
from .runtime import PermissionDenied

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # PCM16 mono, little-endian


class Pcm16Frame(BaseModel):
    """A single ordered, 20-ms PCM frame. Source is bound by the trusted host."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    sequence: int = Field(ge=0)
    pcm16: bytes
    captured_at: datetime

    @field_validator("pcm16")
    @classmethod
    def checked_audio(cls, value: bytes) -> bytes:
        if len(value) != FRAME_BYTES:
            raise ValueError(f"expected exactly {FRAME_BYTES} PCM16 bytes")
        return value

    @field_validator("captured_at")
    @classmethod
    def checked_capture_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value


class StreamConfig(BaseModel):
    """Conservative initial defaults, subject to hardware calibration."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rms_threshold: int = Field(default=550, ge=1, le=10_000)
    activation_frames: int = Field(default=3, ge=1, le=15)
    preroll_frames: int = Field(default=6, ge=1, le=25)
    endpoint_silence_frames: int = Field(default=30, ge=5, le=150)
    preview_first_frames: int = Field(default=50, ge=10, le=500)
    preview_every_frames: int = Field(default=60, ge=10, le=500)
    max_utterance_frames: int = Field(default=1500, ge=100, le=3000)
    max_idle_frames: int = Field(default=500, ge=30, le=3000)

    @model_validator(mode="after")
    def logical_bounds(self):
        if self.preroll_frames < self.activation_frames:
            raise ValueError("preroll must cover activation")
        if self.endpoint_silence_frames >= self.max_utterance_frames:
            raise ValueError("endpoint silence must be shorter than max utterance")
        return self


class StreamStatus(str, Enum):
    LISTENING = "LISTENING"
    RECORDING = "RECORDING"
    FINAL_PENDING = "FINAL_PENDING"
    COMMITTED = "COMMITTED"
    NO_SPEECH = "NO_SPEECH"
    NO_TRANSCRIPT = "NO_TRANSCRIPT"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class DecodeRequest:
    """Transient bounded audio snapshot. Never journal or send to Cognition."""

    utterance_id: UUID
    revision: int
    status: Literal["PARTIAL", "FINAL"]
    captured_at: datetime
    pcm16: bytes = field(repr=False)


@dataclass(frozen=True)
class DecodeOutcome:
    disposition: Literal["PROVISIONAL", "COMMITTED", "NO_TRANSCRIPT", "DROPPED"]
    update: TranscriptUpdate | None = None
    committed: TranscriptIngressResult | None = None


class SpeechRecognizer(Protocol):
    """Provider-specific decode; called outside DB locks and transactions."""

    def transcribe_pcm16(self, pcm16: bytes) -> str:
        ...


def _rms(pcm16: bytes) -> int:
    """Fixed-endian RMS gate. Energy VAD is not speaker authentication."""
    samples = array("h")
    samples.frombytes(pcm16)
    if byteorder != "little":
        samples.byteswap()
    return isqrt(sum(v * v for v in samples) // len(samples))


class VoiceStreamSession:
    """One authenticated bounded utterance; create a new session per utterance.

    accept_frame returns optional immutable DecodeRequests immediately.
    The host may drop interim requests under load, but must process FINAL.
    Late ASR results are fenced by request revision and session status.
    Duplicate/out-of-order PCM frames are rejected, never silently joined.
    """

    def __init__(
        self, actor: TrustedActor, source: VoiceInputSource,
        utterance_id: UUID, config: StreamConfig | None = None,
    ):
        if type(actor) is not TrustedActor:
            raise PermissionDenied("speech session requires authenticated user")
        if type(source) is not VoiceInputSource or type(utterance_id) is not UUID:
            raise TypeError("speech session requires trusted source and utterance ID")
        self.actor = actor
        self.source = source
        self.utterance_id = utterance_id
        self.config = config or StreamConfig()
        self.status = StreamStatus.LISTENING
        self._lock = Lock()
        self._next_sequence = 0
        self._last_capture: datetime | None = None
        self._first_voiced_at: datetime | None = None
        self._preroll: deque[bytes] = deque(maxlen=self.config.preroll_frames)
        self._audio = bytearray()
        self._activation = 0
        self._silence = 0
        self._active_frames = 0
        self._idle_frames = 0
        self._last_preview_frame = 0
        self._revision = 0
        self._last_applied_preview = 0
        self._pending_final_revision: int | None = None

    def cancel(self) -> None:
        """Discard transient state; cannot undo an already committed turn."""
        with self._lock:
            if self.status not in {StreamStatus.COMMITTED, StreamStatus.NO_TRANSCRIPT}:
                self.status = StreamStatus.CANCELLED
                self._audio.clear()
                self._preroll.clear()

    def accept_frame(self, frame: Pcm16Frame) -> DecodeRequest | None:
        """Cheap framed VAD and request admission, never ASR inference."""
        if type(frame) is not Pcm16Frame:
            raise TypeError("PCM frame must satisfy the input schema")
        with self._lock:
            if self.status not in {StreamStatus.LISTENING, StreamStatus.RECORDING}:
                raise RuntimeError("speech session no longer accepts PCM frames")
            if frame.sequence != self._next_sequence:
                raise ValueError("PCM frame sequence gap, duplicate or reordering")
            if self._last_capture is not None and frame.captured_at < self._last_capture:
                raise ValueError("capture timestamps moved backward")
            self._next_sequence += 1
            self._last_capture = frame.captured_at
            voiced = _rms(frame.pcm16) >= self.config.rms_threshold
            if self.status is StreamStatus.LISTENING:
                self._idle_frames += 1
                self._preroll.append(frame.pcm16)
                if voiced:
                    if self._activation == 0:
                        self._first_voiced_at = frame.captured_at
                    self._activation += 1
                else:
                    self._activation = 0
                    self._first_voiced_at = None
                if self._activation >= self.config.activation_frames:
                    self.status = StreamStatus.RECORDING
                    self._audio.extend(b"".join(self._preroll))
                    self._active_frames = len(self._preroll)
                    self._preroll.clear()
                elif self._idle_frames >= self.config.max_idle_frames:
                    self.status = StreamStatus.NO_SPEECH
                    self._preroll.clear()
                return None

            self._audio.extend(frame.pcm16)
            self._active_frames += 1
            self._silence = 0 if voiced else self._silence + 1
            is_terminal = (
                self._silence >= self.config.endpoint_silence_frames
                or self._active_frames >= self.config.max_utterance_frames
            )
            is_preview = (
                self._active_frames >= self.config.preview_first_frames
                and self._active_frames - self._last_preview_frame
                    >= self.config.preview_every_frames
            )
            if not is_terminal and not is_preview:
                return None
            self._revision += 1
            if is_terminal:
                self.status = StreamStatus.FINAL_PENDING
                self._pending_final_revision = self._revision
                self._last_preview_frame = self._active_frames
                snapshot = bytes(self._audio)
                self._audio.clear()
                return DecodeRequest(
                    self.utterance_id, self._revision, "FINAL",
                    self._first_voiced_at or frame.captured_at, snapshot,
                )
            self._last_preview_frame = self._active_frames
            return DecodeRequest(
                self.utterance_id, self._revision, "PARTIAL",
                self._first_voiced_at or frame.captured_at, bytes(self._audio),
            )

    def finish(self) -> DecodeRequest | None:
        """Trusted transport EOF: finalize speech without waiting for silence."""
        with self._lock:
            if self.status is StreamStatus.LISTENING:
                self.status = StreamStatus.NO_SPEECH
                self._preroll.clear()
                return None
            if self.status is not StreamStatus.RECORDING:
                raise RuntimeError("speech session is already terminal")
            self._revision += 1
            self.status = StreamStatus.FINAL_PENDING
            self._pending_final_revision = self._revision
            snapshot = bytes(self._audio)
            self._audio.clear()
            assert self._first_voiced_at is not None
            return DecodeRequest(
                self.utterance_id, self._revision, "FINAL",
                self._first_voiced_at, snapshot,
            )

    def apply_decode(
        self, connection: Connection | None, request: DecodeRequest, text: str,
    ) -> DecodeOutcome:
        """Fence late previews; persist FINAL only through P5-01 ingress.

        The provider call MUST finish before this function. Failed final
        commits remain retryable with the same request and ASR result.
        """
        if not isinstance(request, DecodeRequest) or not isinstance(text, str):
            raise TypeError("invalid ASR completion")
        if request.utterance_id != self.utterance_id:
            raise ValueError("ASR result belongs to another utterance")
        with self._lock:
            if request.status == "PARTIAL":
                if (self.status is not StreamStatus.RECORDING
                        or request.revision > self._revision
                        or request.revision <= self._last_applied_preview):
                    return DecodeOutcome("DROPPED")
                self._last_applied_preview = request.revision
                if not text.strip():
                    return DecodeOutcome("DROPPED")
                update = TranscriptUpdate(
                    utterance_id=self.utterance_id,
                    revision=request.revision, status="PARTIAL",
                    text=text, captured_at=request.captured_at,
                )
                return DecodeOutcome("PROVISIONAL", update=update)
            if (request.status != "FINAL"
                    or self.status is not StreamStatus.FINAL_PENDING
                    or request.revision != self._pending_final_revision):
                return DecodeOutcome("DROPPED")
            if not text.strip():
                self.status = StreamStatus.NO_TRANSCRIPT
                return DecodeOutcome("NO_TRANSCRIPT")
            update = TranscriptUpdate(
                utterance_id=self.utterance_id, revision=request.revision,
                status="FINAL", text=text, captured_at=request.captured_at,
            )
            committed = ingest_voice_transcript(connection, self.actor, self.source, update)
            self.status = StreamStatus.COMMITTED
            return DecodeOutcome("COMMITTED", update=update, committed=committed)


def recognize_request(recognizer: SpeechRecognizer, request: DecodeRequest) -> str:
    """Explicit provider boundary: call from a bounded host executor outside DB."""
    return recognizer.transcribe_pcm16(request.pcm16)
