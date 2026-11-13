"""P5-04 in-memory natural voice turn-taking and conservative echo gating.

No model inference, PostgreSQL work or external playback inside PCM admission.
Hosts must bind identity/channel, provide genuine AEC when configured, and
stop/flush local playback immediately on interrupt_requested.
"""

from datetime import datetime
from enum import Enum
from threading import Event, Lock
from uuid import UUID

from .contracts import TrustedActor
from .multimodal_contracts import VoiceInputSource
from .runtime import PermissionDenied
from .speech_output_contracts import SpeechOutputTarget
from .streaming_asr import Pcm16Frame, StreamStatus, VoiceStreamSession
from .turn_taking_contracts import (
    SpeechInterruptCommand, TurnFrameResult,
)


class EchoPolicy(str, Enum):
    """Trusted host capability, never inferred from model/transcript content."""

    BLOCK_MIC_DURING_OUTPUT = "BLOCK_MIC_DURING_OUTPUT"
    VERIFIED_AEC = "VERIFIED_AEC"


class TurnTakingController:
    """One microphone utterance and one response playback binding.

    BLOCK_MIC_DURING_OUTPUT is the safe default: cannot barge in but prevents
    self-TTS from becoming a user turn. VERIFIED_AEC requires that the trusted
    media host actually removes playback loopback before presenting frames.
    RMS/VAD by itself CANNOT distinguish real user speech from echoed TTS.

    Speech onset sets a per-response cancellation Event. The controller does
    not call the database or infer user intent from partial ASR hypotheses.
    """

    def __init__(
        self,
        session: VoiceStreamSession,
        response_id: UUID,
        target: SpeechOutputTarget,
        *,
        echo_policy: EchoPolicy = EchoPolicy.BLOCK_MIC_DURING_OUTPUT,
        post_playback_guard_frames: int = 10,
    ):
        if type(session) is not VoiceStreamSession:
            raise TypeError("voice session must be authenticated and bound")
        if type(response_id) is not UUID or type(target) is not SpeechOutputTarget:
            raise TypeError("turn-taking requires trusted response/channel binding")
        if type(echo_policy) is not EchoPolicy:
            raise TypeError("echo policy must be selected by authenticated host")
        if not 0 <= post_playback_guard_frames <= 50:
            raise ValueError("post-playback echo guard must be bounded")
        self.session = session
        self.response_id = response_id
        self.target = target
        self.echo_policy = echo_policy
        self._lock = Lock()
        self._playback_active = False
        self._next_external_sequence = 0
        self._next_voice_sequence = 0
        self._last_capture: datetime | None = None
        self._guard_frames = post_playback_guard_frames
        self._remaining_guard_frames = 0
        self._barge_in_detected = False
        self.cancellation = Event()

    @property
    def barge_in_detected(self) -> bool:
        return self._barge_in_detected

    def set_playback_active(self, active: bool) -> bool:
        """Guard physical playback start/resume; return True if it must stop.

        The trusted host MUST check this return value before starting/resuming
        audio. True means flush the output queue and persist interrupt_command().
        This closes the race where speech started during a playback gap:
        no new VAD onset will occur because the ASR session is recording.
        """
        if type(active) is not bool:
            raise TypeError("playback status must be bool")
        with self._lock:
            if active and self._barge_in_detected:
                raise RuntimeError("interrupted response must never restart playback")
            if active and self.session.status in {
                StreamStatus.RECORDING, StreamStatus.FINAL_PENDING,
                StreamStatus.COMMITTED,
            }:
                # Accepted user speech always outranks output, even under the
                # default half-duplex echo policy. Do not activate playback.
                self._barge_in_detected = True
                self.cancellation.set()
                self._remaining_guard_frames = 0
                return True
            if self._playback_active and not active and not self._barge_in_detected:
                if self.session.status is StreamStatus.LISTENING:
                    self._remaining_guard_frames = self._guard_frames
            self._playback_active = active
            return False

    def accept_microphone_frame(self, frame: Pcm16Frame) -> TurnFrameResult:
        """Bounded 20-ms processing; no DB, TTS, ASR or media callbacks."""
        if type(frame) is not Pcm16Frame:
            raise TypeError("invalid microphone PCM frame")
        with self._lock:
            if frame.sequence != self._next_external_sequence:
                raise ValueError("microphone frame gap, duplicate or reordering")
            if self._last_capture is not None and frame.captured_at < self._last_capture:
                raise ValueError("microphone capture time moved backward")
            self._next_external_sequence += 1
            self._last_capture = frame.captured_at

            if (self._playback_active
                    and self.echo_policy is EchoPolicy.BLOCK_MIC_DURING_OUTPUT):
                return TurnFrameResult(suppressed_echo_risk=True)
            if not self._playback_active and self._remaining_guard_frames:
                self._remaining_guard_frames -= 1
                return TurnFrameResult(suppressed_echo_risk=True)

            # Frames may be suppressed while a player is speaking. The ASR
            # session receives contiguous *local* sequences only.
            forwarded = Pcm16Frame(
                sequence=self._next_voice_sequence,
                pcm16=frame.pcm16, captured_at=frame.captured_at,
            )
            was_listening = self.session.status is StreamStatus.LISTENING
            request = self.session.accept_frame(forwarded)
            self._next_voice_sequence += 1
            speech_started = (
                was_listening
                and self.session.status is StreamStatus.RECORDING
            )
            barge_in = bool(
                speech_started and self._playback_active
                and self.echo_policy is EchoPolicy.VERIFIED_AEC
                and not self._barge_in_detected
            )
            if barge_in:
                self._barge_in_detected = True
                self.cancellation.set()
            return TurnFrameResult(
                speech_started=speech_started,
                interrupt_requested=barge_in,
                decode_request=request,
            )

    def interrupt_command(self) -> SpeechInterruptCommand:
        """Construct only after verified AEC speech onset while TTS is playing."""
        with self._lock:
            if not self._barge_in_detected:
                raise RuntimeError("no accepted speech barge-in to persist")
            return SpeechInterruptCommand(
                response_id=self.response_id,
                conversation_id=self.session.source.conversation_id,
                instance_id=self.session.source.instance_id,
                expected_generation=self.session.source.expected_generation,
                output_channel_id=self.target.channel_id,
                input_channel_id=self.session.source.channel_id,
                reason="USER_SPEECH",
                utterance_id=self.session.utterance_id,
            )

    def stop(self) -> None:
        """Stop this response only; do not cancel persona tasks/operations."""
        with self._lock:
            if self._playback_active and not self._barge_in_detected:
                # Manual output stop needs the same tail-echo guard as a
                # natural playback completion; barge-in must not lose speech.
                self._remaining_guard_frames = self._guard_frames
            self._playback_active = False
            self.cancellation.set()

    def close_microphone(self) -> None:
        """Cancel stale ASR previews/results without changing conversation."""
        self.session.cancel()
