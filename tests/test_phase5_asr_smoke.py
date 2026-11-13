"""P5-02 WAV smoke regressions for terminal no-speech streams."""

import wave
from struct import pack

import pytest

# The established no-psycopg contract job skips DB-integrated modules.
pytest.importorskip("psycopg")

from scripts import phase5_asr_smoke
from persona_agent.streaming_asr import FRAME_BYTES


@pytest.mark.parametrize(
    "amplitude,late_voice",
    [(0, False), (250, False), (0, True)],
    ids=["overlong-silence", "subthreshold-audio", "silence-before-voice"],
)
def test_wav_smoke_stops_on_no_speech_without_decoding(
    tmp_path, capsys, monkeypatch, amplitude, late_voice,
):
    """No ASR request or RuntimeError after the 500-frame idle timeout."""
    wav_path = tmp_path / "long-leading-silence.wav"
    below_threshold = pack("<h", amplitude) * (FRAME_BYTES // 2)
    later_voice = pack("<h", 3000) * (FRAME_BYTES // 2)
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        # 520 frames = 10.4 seconds, beyond the 500-frame idle budget.
        # In the third case, voice begins too late for this session.
        output.writeframes(
            below_threshold * 520
            + (later_voice * 50 if late_voice else b"")
        )

    def should_not_decode(*_args, **_kwargs):
        pytest.fail("idle audio must not produce an ASR decode request")

    monkeypatch.setattr(phase5_asr_smoke, "recognize_request", should_not_decode)
    assert phase5_asr_smoke.main([str(wav_path), "--model", "tiny"]) == 1
    assert capsys.readouterr().out.strip() == "No finalized speech detected"


def test_wav_smoke_still_finalizes_valid_speech(tmp_path, capsys, monkeypatch):
    """The early idle exit must not suppress ordinary voiced WAV input."""
    wav_path = tmp_path / "short-speech.wav"
    voiced = pack("<h", 3000) * (FRAME_BYTES // 2)
    silent = bytes(FRAME_BYTES)
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(voiced * 55 + silent * 35)

    calls = []
    def fake_decode(_recognizer, request):
        calls.append(request)
        return "recognized voice"

    monkeypatch.setattr(phase5_asr_smoke, "recognize_request", fake_decode)
    assert phase5_asr_smoke.main([str(wav_path), "--model", "tiny"]) == 0
    assert capsys.readouterr().out.strip() == "recognized voice"
    assert len(calls) == 1 and calls[0].status == "FINAL"


@pytest.mark.parametrize(
    "sample_count",
    [16_160, 16_001],
    ids=["one-point-zero-one-second-voice", "one-sample-past-full-frame"],
)
def test_wav_smoke_pads_partial_final_frame_and_finalizes_at_eof(
    tmp_path, capsys, monkeypatch, sample_count,
):
    """Valid PCM16 WAVs need not end exactly on a 320-sample boundary."""
    wav_path = tmp_path / "non-aligned-voice.wav"
    original_pcm = pack("<h", 3000) * sample_count
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(original_pcm)

    # The ASR adapter must get every original sample in order, followed
    # by silence padding only within the final 20-ms frame.
    expected_bytes = original_pcm.ljust(
        ((len(original_pcm) + FRAME_BYTES - 1) // FRAME_BYTES) * FRAME_BYTES,
        b"\x00",
    )
    observed = []

    def fake_decode(_recognizer, request):
        observed.append(request)
        assert request.status == "FINAL"
        assert request.pcm16 == expected_bytes
        return "valid short recording"

    monkeypatch.setattr(phase5_asr_smoke, "recognize_request", fake_decode)
    assert phase5_asr_smoke.main([str(wav_path), "--model", "tiny"]) == 0
    assert capsys.readouterr().out.strip() == "valid short recording"
    assert len(observed) == 1
