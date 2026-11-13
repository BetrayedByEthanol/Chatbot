"""P5-03 bounded speech clipping and validated PCM16 WAV.

A response is emitted one speech clip at a time, so playback can start before
the entire response is synthesized. This is CLIP-level streaming, not native
sample/token streaming from the TTS engine.
"""

import hashlib
import io
import re
import wave
from dataclasses import dataclass

from .speech_output_contracts import InvalidSpeechAudio

MAX_CLIPS = 64
MAX_CHARS = 220
MAX_AUDIO_MS = 30_000
VALID_RATES = frozenset({16000, 22050, 24000, 48000})
MAX_AUDIO_BYTES = max(VALID_RATES) * 2 * MAX_AUDIO_MS // 1000 + 8192


@dataclass(frozen=True)
class AudioInfo:
    sample_rate_hz: int
    duration_ms: int
    payload_sha256: str


def split_speech_text(text: str, *, max_chars: int = MAX_CHARS) -> tuple[str, ...]:
    """Bounded, deterministic sentence-aware splitting. Never invent content."""
    if not 40 <= max_chars <= 500 or not text or len(text) > 12000:
        raise ValueError("invalid speech response length or clip bound")
    tokens = re.findall(r"\S+", text)
    if not tokens:
        raise ValueError("speech response must have words")
    parts: list[str] = []
    current = ""
    for token in tokens:
        if len(token) > max_chars:
            raise ValueError("unbreakable speech token exceeds clip limit")
        if current and len(current) + len(token) + 1 > max_chars:
            parts.append(current)
            current = ""
        current = (current + " " + token).strip()
        # Prefer sentence edges once a moderate chunk has accumulated.
        if len(current) >= max_chars // 2 and re.search(r"[.!?][\"']?$", token):
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    if len(parts) > MAX_CLIPS:
        raise ValueError("speech response exceeds maximum clip count")
    return tuple(parts)


def validate_wav_pcm16(audio_wav: bytes) -> AudioInfo:
    """Reject corrupt RIFF sizes, unsupported encodings and truncated payloads."""
    if not isinstance(audio_wav, bytes) or not (44 <= len(audio_wav) <= MAX_AUDIO_BYTES):
        raise InvalidSpeechAudio("WAV payload missing or exceeds bounded clip size")
    if audio_wav[:4] != b"RIFF" or audio_wav[8:12] != b"WAVE":
        raise InvalidSpeechAudio("expected RIFF/WAVE payload")
    declared_length = int.from_bytes(audio_wav[4:8], "little")
    if declared_length != len(audio_wav) - 8:
        raise InvalidSpeechAudio("RIFF byte length does not match payload")
    try:
        with wave.open(io.BytesIO(audio_wav), "rb") as wav:
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getcomptype() != "NONE"
                    or wav.getframerate() not in VALID_RATES):
                raise InvalidSpeechAudio("unsupported WAV encoding or sample rate")
            frames = wav.getnframes()
            raw = wav.readframes(frames)
            if frames == 0 or len(raw) != frames * 2:
                raise InvalidSpeechAudio("WAV frames do not match declared duration")
            rate = wav.getframerate()
    except (wave.Error, EOFError, ValueError) as exc:
        if isinstance(exc, InvalidSpeechAudio):
            raise
        raise InvalidSpeechAudio("malformed PCM16 WAV header") from exc
    duration_ms = (frames * 1000 + rate - 1) // rate
    if not 1 <= duration_ms <= MAX_AUDIO_MS:
        raise InvalidSpeechAudio("WAV clip exceeds duration limit")
    return AudioInfo(rate, duration_ms, hashlib.sha256(audio_wav).hexdigest())
