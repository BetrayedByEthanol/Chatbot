"""Optional local Piper CLI TTS adapter, one bounded speech segment per call.

The CLI must be operator-provisioned with a locally trusted model path.
The model sample rate must match the explicitly configured output format.
No shell execution, cloud fallback, auto-download or DB locks.
"""

import io
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .speech_output_audio import MAX_AUDIO_BYTES, VALID_RATES
from .speech_output_contracts import InvalidSpeechAudio


@dataclass(frozen=True)
class PiperCapabilities:
    format: Literal["WAV_PCM16_MONO"] = "WAV_PCM16_MONO"
    streaming_granularity: Literal["CLIP"] = "CLIP"
    local_inference: bool = True
    can_interrupt_between_clips: bool = True
    measured_p95_ms: int | None = None


@dataclass(frozen=True)
class PiperCliSynthesizer:
    model_path: str
    sample_rate_hz: int
    executable: str = "piper"
    timeout_seconds: int = 30

    def __post_init__(self):
        if (not self.model_path or not Path(self.model_path).is_file()
                or self.sample_rate_hz not in VALID_RATES
                or not self.executable or len(self.executable) > 256
                or not 1 <= self.timeout_seconds <= 120):
            raise ValueError("invalid local Piper configuration")

    @property
    def capabilities(self) -> PiperCapabilities:
        return PiperCapabilities()

    def synthesize_wav(self, text: str) -> bytes:
        if not text.strip() or len(text) > 500:
            raise ValueError("Piper expects a bounded nonempty speech segment")
        try:
            # Piper --output-raw produces PCM16 mono at the selected model's
            # native sample rate. Operators must configure that rate correctly.
            completed = subprocess.run(
                [self.executable, "--model", self.model_path, "--output-raw"],
                input=(text + "\n").encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("local TTS provider unavailable") from exc
        raw = completed.stdout
        if completed.returncode != 0 or not raw:
            raise RuntimeError("local TTS synthesis failed")
        if len(raw) % 2 or len(raw) > MAX_AUDIO_BYTES - 8192:
            raise InvalidSpeechAudio("local TTS PCM16 exceeded bounds or is misaligned")
        with io.BytesIO() as buf:
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate_hz)
                wav.writeframes(raw)
            return buf.getvalue()
