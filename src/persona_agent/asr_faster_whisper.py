"""Optional local faster-whisper inference adapter for P5-02.

A host accepts PCM frames and requests periodic interim snapshots. The
provider decodes bounded snapshots, NOT native token-streaming Whisper output.
Install optional [asr] dependencies and provision a model explicitly.
"""

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .streaming_asr import FRAME_BYTES


@dataclass
class FasterWhisperRecognizer:
    model_size_or_path: str
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None
    beam_size: int = 1
    _model: Any = field(default=None, init=False, repr=False)
    _model_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.model_size_or_path or len(self.model_size_or_path) > 256:
            raise ValueError("invalid ASR model identifier")
        if not 1 <= self.beam_size <= 8:
            raise ValueError("beam_size must be 1..8")
        if self.language is not None and not 1 <= len(self.language) <= 16:
            raise ValueError("invalid ASR language")

    def _load_model(self):
        with self._model_lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise RuntimeError(
                        'Local speech recognition requires pip install -e ".[asr]"'
                    ) from exc
                self._model = WhisperModel(
                    self.model_size_or_path,
                    device=self.device, compute_type=self.compute_type,
                )
            return self._model

    def transcribe_pcm16(self, pcm16: bytes) -> str:
        if not pcm16 or len(pcm16) % FRAME_BYTES:
            raise ValueError("ASR expects complete 20-ms PCM16 frames")
        if len(pcm16) > 3000 * FRAME_BYTES:
            raise ValueError("ASR audio exceeds the bounded maximum")
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Speech recognition requires the [asr] extra") from exc
        waveform = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        model = self._load_model()
        segments, _info = model.transcribe(
            waveform, language=self.language, beam_size=self.beam_size,
            vad_filter=False, condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if len(text) > 12000:
            raise ValueError("ASR result exceeds conversation text limit")
        return text
