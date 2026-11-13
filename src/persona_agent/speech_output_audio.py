"""P5-03 bounded speech clipping and validated PCM16 WAV.

A response is emitted one speech clip at a time, so playback can start before
the entire response is synthesized. This is CLIP-level streaming, not native
sample/token streaming from the TTS engine.
"""

import hashlib
import io
import unicodedata
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
    """Deterministic multilingual sentence-first clipping with bounded fallback.

    Prefer punctuation from both spaced and unspaced writing systems. Preserve
    text except for the existing whitespace normalization; never insert spaces
    between Chinese/Japanese characters at a forced clip boundary.
    """
    if not 40 <= max_chars <= 500 or not text or len(text) > 12000:
        raise ValueError("invalid speech response length or clip bound")
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("speech response must have words")
    if len(normalized) > MAX_CLIPS * max_chars:
        raise ValueError("speech response exceeds maximum clip capacity")

    stops = frozenset(".!?。！？؟")
    closers = frozenset('”’」』）】》〉)]}') | frozenset(('"', "'"))
    clauses = frozenset("、，,;；：:")
    parts: list[str] = []
    start = 0
    # Short sentences can remain distinct, but very long responses must pack
    # sentences to avoid exceeding the 64-clip delivery contract.
    minimum = max(1, (len(normalized) + MAX_CLIPS - 1) // MAX_CLIPS)
    while start < len(normalized):
        end = min(start + max_chars, len(normalized))
        sentence_end = None
        clause_end = None
        for pos in range(start, end):
            char = normalized[pos]
            # Do not treat decimal points or Latin word-internal dots as
            # sentence boundaries (3.14, example.com).
            if char == "." and 0 < pos < len(normalized) - 1 and (
                (normalized[pos - 1].isdigit() and normalized[pos + 1].isdigit())
                or (
                    normalized[pos - 1].isascii()
                    and normalized[pos - 1].isalpha()
                    and normalized[pos + 1].isascii()
                    and normalized[pos + 1].isalpha()
                )
            ):
                continue
            if char in stops:
                boundary = pos + 1
                while boundary < end and normalized[boundary] in closers:
                    boundary += 1
                if boundary - start >= minimum:
                    sentence_end = boundary
                    break
            elif char in clauses:
                clause_end = pos + 1

        if sentence_end is not None:
            end = sentence_end
        elif end < len(normalized):
            # Prefer a word or clause boundary; otherwise use a hard length
            # limit so paragraphs without whitespace remain speakable.
            lower = start + minimum
            last_space = normalized.rfind(" ", lower, end + 1)
            if last_space > start:
                end = last_space
            elif clause_end is not None and clause_end >= lower:
                end = clause_end
            else:
                # Keep combining accents, variation selectors and ZWJ
                # sequences attached to their preceding base characters.
                while end > start and (
                    unicodedata.combining(normalized[end])
                    or normalized[end] in ("\ufe0e", "\ufe0f", "\u200d")
                    or normalized[end - 1] == "\u200d"
                ):
                    end -= 1
                if end == start:
                    raise ValueError("speech grapheme exceeds clip size")

        clip = normalized[start:end].strip()
        if not clip:
            raise ValueError("speech clipping produced empty text")
        parts.append(clip)
        if len(parts) > MAX_CLIPS:
            raise ValueError("speech response exceeds maximum clip count")
        start = end
        while start < len(normalized) and normalized[start] == " ":
            start += 1
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
