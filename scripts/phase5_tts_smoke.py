"""P5-03 local Piper TTS smoke. Does not access persona or PostgreSQL.

Example:
 python scripts/phase5_tts_smoke.py --model /path/voice.onnx \
   --sample-rate 22050 --text "Hello." --output /tmp/speech.wav
"""

import argparse
from pathlib import Path

from persona_agent.speech_output_audio import validate_wav_pcm16
from persona_agent.tts_piper import PiperCliSynthesizer


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",required=True)
    parser.add_argument("--sample-rate",type=int,required=True)
    parser.add_argument("--text",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--device",default=None,help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    synth=PiperCliSynthesizer(args.model,args.sample_rate)
    wav=synth.synthesize_wav(args.text)
    info=validate_wav_pcm16(wav)
    args.output.write_bytes(wav)
    print(f"Validated {info.sample_rate_hz} Hz PCM16 WAV, {info.duration_ms} ms")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
