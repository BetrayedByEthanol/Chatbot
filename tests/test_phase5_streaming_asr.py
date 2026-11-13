"""P5-02: bounded PCM stream, VAD, ASR previews/finals and shared context."""

from datetime import datetime, timedelta, timezone
from struct import pack
from uuid import uuid4

import pytest
from pydantic import ValidationError

# The Phase 0 no-psycopg CI job must skip DB-integrated modules on collection.
psycopg = pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.multimodal_contracts import VoiceInputSource
from persona_agent.streaming_asr import (
    FRAME_BYTES, DecodeRequest, Pcm16Frame, StreamConfig, StreamStatus,
    VoiceStreamSession, recognize_request,
)
from persona_agent.asr_faster_whisper import FasterWhisperRecognizer

CAPTURE = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
SILENCE = bytes(FRAME_BYTES)
VOICE = pack("<h", 3000) * (FRAME_BYTES // 2)


def session(config=None):
    actor = TrustedActor(user_id=uuid4(), source_id="authenticated-voice-host")
    source = VoiceInputSource(
        conversation_id=uuid4(), instance_id=uuid4(),
        expected_generation=1, channel_id="trusted-mic",
    )
    return VoiceStreamSession(actor, source, uuid4(), config)


def frame(sequence, pcm=VOICE, captured_at=None):
    return Pcm16Frame(
        sequence=sequence, pcm16=pcm,
        captured_at=captured_at or CAPTURE + timedelta(milliseconds=20 * sequence),
    )


class StubASR:
    def __init__(self, text="spoken words"):
        self.text = text
        self.calls = []

    def transcribe_pcm16(self, pcm16):
        self.calls.append(pcm16)
        return self.text


def test_schema_and_pcm_ordering():
    with pytest.raises(ValidationError):
        frame(0, b"invalid")
    with pytest.raises(ValidationError):
        Pcm16Frame(sequence=0, pcm16=VOICE, captured_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        Pcm16Frame(sequence=0, pcm16=VOICE, captured_at=CAPTURE, user_id=uuid4())
    s = session()
    s.accept_frame(frame(0))
    with pytest.raises(ValueError, match="sequence"):
        s.accept_frame(frame(0))
    with pytest.raises(ValueError, match="sequence"):
        s.accept_frame(frame(2))
    with pytest.raises(ValueError, match="backward"):
        s.accept_frame(frame(1, captured_at=CAPTURE - timedelta(seconds=1)))
    assert s.accept_frame(frame(1)) is None


def test_noise_only_closes_without_asr_or_db():
    s = session(StreamConfig(max_idle_frames=30))
    for n in range(30):
        assert s.accept_frame(frame(n, SILENCE)) is None
    assert s.status is StreamStatus.NO_SPEECH
    with pytest.raises(RuntimeError):
        s.accept_frame(frame(30))


def test_speech_vad_partial_and_final_end_silence():
    config = StreamConfig(
        activation_frames=2, preroll_frames=3,
        endpoint_silence_frames=5,
        preview_first_frames=10, preview_every_frames=10,
        max_utterance_frames=100, max_idle_frames=50,
    )
    s = session(config)
    for n in range(20):
        req = s.accept_frame(frame(n, VOICE if n >= 1 else SILENCE))
        if req is not None:
            break
    assert req is not None and req.status == "PARTIAL"
    assert req.revision == 1 and req.utterance_id == s.utterance_id
    assert len(req.pcm16) >= 10 * FRAME_BYTES
    fake = StubASR("hello so far")
    text = recognize_request(fake, req)
    partial = s.apply_decode(None, req, text)
    assert partial.disposition == "PROVISIONAL"
    assert partial.update is not None and partial.update.status == "PARTIAL"
    assert s.apply_decode(None, req, text).disposition == "DROPPED"
    last = None
    for n in range(s._next_sequence, s._next_sequence + 5):
        last = s.accept_frame(frame(n, SILENCE))
    assert last is not None and last.status == "FINAL"
    assert s.status is StreamStatus.FINAL_PENDING
    assert s.apply_decode(None, req, "late preview").disposition == "DROPPED"
    assert s.apply_decode(None, last, "").disposition == "NO_TRANSCRIPT"
    assert s.status is StreamStatus.NO_TRANSCRIPT


def test_stream_limit_and_transport_finish():
    s = session(StreamConfig(max_utterance_frames=100))
    for n in range(3):
        s.accept_frame(frame(n))
    done = s.finish()
    assert done is not None and done.status == "FINAL"
    assert len(done.pcm16) == 3 * FRAME_BYTES
    with pytest.raises(RuntimeError):
        s.finish()
    s2 = session(StreamConfig(max_utterance_frames=100))
    request = None
    for n in range(100):
        request = s2.accept_frame(frame(n))
        if request is not None and request.status == "FINAL":
            break
    assert request is not None and request.status == "FINAL"
    assert len(request.pcm16) <= 100 * FRAME_BYTES


def test_cancel_drops_late_asr():
    s = session()
    for n in range(3):
        s.accept_frame(frame(n))
    request = s.finish()
    s.cancel()
    assert s.apply_decode(None, request, "late result").disposition == "DROPPED"
    assert s.status is StreamStatus.CANCELLED


def test_stale_partial_revision_and_wrong_utterance():
    config = StreamConfig(
        activation_frames=1, preroll_frames=1, preview_first_frames=10,
        preview_every_frames=10, max_utterance_frames=100,
    )
    s = session(config)
    requests = []
    for n in range(26):
        req = s.accept_frame(frame(n))
        if req is not None:
            requests.append(req)
    assert len(requests) == 2
    assert s.apply_decode(None, requests[1], "newer").disposition == "PROVISIONAL"
    assert s.apply_decode(None, requests[0], "older").disposition == "DROPPED"
    spoof = DecodeRequest(uuid4(), requests[1].revision, "PARTIAL", CAPTURE, VOICE)
    with pytest.raises(ValueError, match="another utterance"):
        s.apply_decode(None, spoof, "no")


def test_local_provider_rejects_bad_pcm_without_optional_import():
    provider = FasterWhisperRecognizer("tiny")
    with pytest.raises(ValueError, match="complete"):
        provider.transcribe_pcm16(b"bad")
    with pytest.raises(ValueError):
        FasterWhisperRecognizer("", beam_size=1)
    with pytest.raises(ValueError):
        FasterWhisperRecognizer("tiny", beam_size=0)


def test_local_provider_waveform_and_cached_model(monkeypatch):
    import sys
    from types import SimpleNamespace
    np = pytest.importorskip("numpy")
    class FakeModel:
        calls = 0
        def __init__(self, *args, **kwargs):
            self.options = kwargs
        def transcribe(self, array, **kwargs):
            FakeModel.calls += 1
            assert array.dtype == np.float32
            assert array.shape == (FRAME_BYTES // 2,)
            assert 0 < float(array.max()) <= 1
            assert kwargs["condition_on_previous_text"] is False
            assert kwargs["vad_filter"] is False
            return iter([SimpleNamespace(text="hello there")]), None
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    provider = FasterWhisperRecognizer("tiny", language="en")
    assert provider.transcribe_pcm16(VOICE) == "hello there"
    assert provider.transcribe_pcm16(VOICE) == "hello there"
    assert FakeModel.calls == 2
    assert provider._model is not None


def test_final_decode_failure_remains_retryable(monkeypatch):
    from persona_agent import streaming_asr
    s = session()
    for n in range(3):
        s.accept_frame(frame(n))
    request = s.finish()
    called = []
    def flaky(*args):
        called.append(1)
        if len(called) == 1:
            raise TimeoutError("temporary database error")
        return object()
    monkeypatch.setattr(streaming_asr, "ingest_voice_transcript", flaky)
    with pytest.raises(TimeoutError):
        s.apply_decode(None, request, "spoken final")
    assert s.status is StreamStatus.FINAL_PENDING
    outcome = s.apply_decode(None, request, "spoken final")
    assert outcome.disposition == "COMMITTED"
    assert len(called) == 2


def test_final_commit_reuses_p501_event_and_context(dsn):
    psycopg = pytest.importorskip("psycopg")
    from test_phase1_conversation import actor, call, seed, FakeGateway
    from persona_agent.conversation import (
        acknowledge_text_delivery, prepare_text_context, respond_to_text, ingest_text,
    )
    from persona_agent.conversation_contracts import TextInputCommand
    user, instance = seed(dsn)
    conv = uuid4()
    src = VoiceInputSource(
        conversation_id=conv, instance_id=instance,
        expected_generation=1, channel_id="trusted-mic",
    )
    s = VoiceStreamSession(actor(user), src, uuid4())
    for n in range(3):
        s.accept_frame(frame(n))
    request = s.finish()
    with psycopg.connect(dsn, autocommit=True) as conn:
        result = s.apply_decode(conn, request, recognize_request(StubASR("hello voice"), request))
    assert result.disposition == "COMMITTED"
    assert result.committed.turn_id and s.status is StreamStatus.COMMITTED
    assert s.apply_decode(None, request, "late final").disposition == "DROPPED"
    with psycopg.connect(dsn, autocommit=True) as conn:
        rec = conn.execute(
            "SELECT event_type,payload FROM events WHERE event_id=%s",
            (result.committed.event_id,),
        ).fetchone()
        assert rec[0] == "conversation.user_text.received.v1"
        assert rec[1]["input_modality"] == "VOICE"
        assert rec[1]["utterance_id"] == str(s.utterance_id)
        assert rec[1]["capture_time_basis"] == "SOURCE_REPORTED"
    reply = call(dsn, respond_to_text, actor(user), result.committed.turn_id, FakeGateway("Hello back"))
    call(dsn, acknowledge_text_delivery, actor(user), reply.response_id)
    typed = call(dsn, ingest_text, actor(user), TextInputCommand(
        conversation_id=conv, instance_id=instance, expected_generation=1,
        idempotency_key=str(uuid4()), text="Continue typed",
    ))
    messages = call(dsn, prepare_text_context, actor(user), typed.turn_id).messages
    assert [(m.role, m.content) for m in messages[1:]] == [
        ("user", "hello voice"), ("assistant", "Hello back"),
        ("user", "Continue typed"),
    ]


def test_inflight_asr_fenced_by_persona_switch(dsn):
    psycopg = pytest.importorskip("psycopg")
    from test_phase1_conversation import seed, actor, call
    from persona_agent.lifecycle import switch_persona, SwitchPersonaCommand
    from persona_agent.runtime import StaleSelection
    user, instance, other = seed(dsn, two=True)
    src = VoiceInputSource(
        conversation_id=uuid4(), instance_id=instance,
        expected_generation=1, channel_id="trusted-mic",
    )
    s = VoiceStreamSession(actor(user), src, uuid4())
    for n in range(3):
        s.accept_frame(frame(n))
    request = s.finish()
    call(dsn, switch_persona, actor(user), SwitchPersonaCommand(
        expected_instance_id=instance,
        target_instance_id=other,
        expected_generation=1,
        idempotency_key=str(uuid4()),
    ))
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(StaleSelection):
            s.apply_decode(conn, request, "not authorized now")
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (src.conversation_id,),
        ).fetchone()[0] == 0
    assert s.status is StreamStatus.FINAL_PENDING
