"""P5-03 speech WAV validation, clip segmentation and local Piper adapter."""

import io
import subprocess
import wave
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.speech_output_audio import split_speech_text, validate_wav_pcm16
from persona_agent.speech_output_contracts import InvalidSpeechAudio
from persona_agent.tts_piper import PiperCliSynthesizer


def wav_bytes(rate=16000, frames=1600, channels=1, width=2):
    with io.BytesIO() as target:
        with wave.open(target, "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(width)
            writer.setframerate(rate)
            writer.writeframes(b"\x01" * (frames * channels * width))
        return target.getvalue()


def test_wav_validation_ensures_real_duration_and_header_integrity():
    wav=wav_bytes(frames=1601)
    info=validate_wav_pcm16(wav)
    assert info.sample_rate_hz==16000
    assert info.duration_ms==101
    assert len(info.payload_sha256)==64
    for invalid in (
        b"junk", wav[:-4], wav+b"\x00", wav_bytes(rate=44100),
        wav_bytes(channels=2), wav_bytes(width=1),
        wav[:4]+(0).to_bytes(4,"little")+wav[8:],
    ):
        with pytest.raises(InvalidSpeechAudio):
            validate_wav_pcm16(invalid)


def test_text_clip_segmentation_is_deterministic_and_bounded():
    text="First sentence. Second sentence! Third sentence?"
    assert split_speech_text(text,max_chars=40)==(
        "First sentence. Second sentence!", "Third sentence?",
    )
    many="hello " * 120
    result=split_speech_text(many)
    assert len(result)>1
    assert all(len(x)<=220 for x in result)
    assert " ".join(result)==" ".join(many.split())
    with pytest.raises(ValueError):
        split_speech_text("x"*221)
    with pytest.raises(ValueError):
        split_speech_text(" ")


def test_piper_adapter_validates_model_and_wraps_pcm_as_wav(tmp_path, monkeypatch):
    model=tmp_path/"voice.onnx"
    model.write_bytes(b"mock-only")
    calls=[]
    def fake_run(argv, **kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,stdout=b"\x01\x00"*1000,stderr=b"")
    monkeypatch.setattr("persona_agent.tts_piper.subprocess.run",fake_run)
    synth=PiperCliSynthesizer(str(model),22050)
    assert synth.capabilities.local_inference
    assert synth.capabilities.streaming_granularity=="CLIP"
    wav=synth.synthesize_wav("Hello")
    assert validate_wav_pcm16(wav).sample_rate_hz==22050
    assert calls[0][0]==["piper","--model",str(model),"--output-raw"]
    assert calls[0][1]["input"]==b"Hello\n"
    assert "shell" not in calls[0][1]
    with pytest.raises(ValueError):
        PiperCliSynthesizer("/missing/file.onnx",16000)


def test_piper_failed_provider_does_not_fabricate_audio(tmp_path, monkeypatch):
    model=tmp_path/"voice.onnx"
    model.write_bytes(b"mock-only")
    monkeypatch.setattr(
        "persona_agent.tts_piper.subprocess.run",
        lambda argv,**kwargs:subprocess.CompletedProcess(argv,1,stdout=b"",stderr=b"fail"),
    )
    with pytest.raises(RuntimeError):
        PiperCliSynthesizer(str(model),16000).synthesize_wav("Hi")


def test_piper_timeout_reports_unavailable_not_success(tmp_path, monkeypatch):
    model=tmp_path/"voice.onnx"
    model.write_bytes(b"mock-only")
    def timed_out(argv,**kwargs):
        raise subprocess.TimeoutExpired(argv,1)
    monkeypatch.setattr("persona_agent.tts_piper.subprocess.run",timed_out)
    with pytest.raises(RuntimeError):
        PiperCliSynthesizer(str(model),16000).synthesize_wav("Hi")
