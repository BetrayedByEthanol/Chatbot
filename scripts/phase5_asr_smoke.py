"""P5-02 optional local ASR WAV smoke; no DB or persona execution authority.

Usage: python scripts/phase5_asr_smoke.py short.wav --model tiny --language en
The WAV must be consented, mono PCM16, 16 kHz.
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import wave
from uuid import uuid4

from persona_agent.asr_faster_whisper import FasterWhisperRecognizer
from persona_agent.contracts import TrustedActor
from persona_agent.multimodal_contracts import VoiceInputSource
from persona_agent.streaming_asr import (
    FRAME_BYTES, Pcm16Frame, StreamStatus, VoiceStreamSession, recognize_request,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--language", default=None)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args(argv)
    recognizer = FasterWhisperRecognizer(
        args.model, device=args.device, compute_type=args.compute_type,
        language=args.language,
    )
    # Artificial IDs exist ONLY for this offline, non-authoritative smoke.
    session = VoiceStreamSession(
        TrustedActor(user_id=uuid4(), source_id="local-wav-smoke-only"),
        VoiceInputSource(
            conversation_id=uuid4(), instance_id=uuid4(),
            expected_generation=1, channel_id="local-wav-smoke-only",
        ),
        utterance_id=uuid4(),
    )
    last_request = None
    start = datetime.now(timezone.utc)
    with wave.open(str(args.wav), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            parser.error("WAV must be mono PCM16 16000 Hz")
        for number in range(2000):
            pcm = audio.readframes(FRAME_BYTES // 2)
            if not pcm:
                break
            # A valid WAV can end between 20-ms frame boundaries. Zero-pad
            # the final PCM16 chunk so the stream can finalize at EOF.
            if len(pcm) < FRAME_BYTES:
                pcm = pcm.ljust(FRAME_BYTES, b"\x00")
            request = session.accept_frame(Pcm16Frame(
                sequence=number, pcm16=pcm,
                captured_at=start + timedelta(milliseconds=20 * number),
            ))
            # NO_SPEECH is terminal. Stop consuming an overlong silent WAV
            # before the next frame could raise on this closed session.
            if session.status is StreamStatus.NO_SPEECH:
                break
            if request is not None:
                last_request = request
                if request.status == "FINAL":
                    break
                # Offline smoke drops optional previews to minimize compute.
    if session.status is StreamStatus.RECORDING:
        last_request = session.finish()
    if last_request is None or last_request.status != "FINAL":
        print("No finalized speech detected")
        return 1
    text = recognize_request(recognizer, last_request)
    print(text or "(Speech detected, but provider returned no transcript)")
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
