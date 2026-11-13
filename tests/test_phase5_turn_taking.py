"""P5-04 in-memory echo gating, speech onset, barge-in and ASR continuity."""

from datetime import datetime, timedelta, timezone
from struct import pack
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.multimodal_contracts import VoiceInputSource
from persona_agent.speech_output_contracts import SpeechOutputTarget
from persona_agent.streaming_asr import (
    FRAME_BYTES, Pcm16Frame, StreamConfig, StreamStatus, VoiceStreamSession,
)
from persona_agent.turn_taking import EchoPolicy, TurnTakingController

SPEECH = pack("<h", 3300) * (FRAME_BYTES // 2)
SILENCE = bytes(FRAME_BYTES)
CAPTURE = datetime(2026, 11, 13, 15, tzinfo=timezone.utc)


def frame(n, pcm=SPEECH):
    return Pcm16Frame(
        sequence=n, pcm16=pcm,
        captured_at=CAPTURE + timedelta(milliseconds=20*n),
    )


def controller(mode=EchoPolicy.BLOCK_MIC_DURING_OUTPUT, guard=3):
    user,instance,conversation = uuid4(),uuid4(),uuid4()
    actor=TrustedActor(user_id=user,source_id="authenticated-voice-host")
    source=VoiceInputSource(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,channel_id="authenticated-microphone",
    )
    config=StreamConfig(
        activation_frames=3,preroll_frames=3,
        endpoint_silence_frames=5,max_idle_frames=100,
        max_utterance_frames=100,preview_first_frames=20,
        preview_every_frames=20,
    )
    session=VoiceStreamSession(actor,source,uuid4(),config)
    return TurnTakingController(
        session,uuid4(),SpeechOutputTarget(channel_id="authenticated-speaker"),
        echo_policy=mode,post_playback_guard_frames=guard,
    )


def test_unverified_mic_during_output_blocks_echo_and_user_turns():
    c=controller()
    c.set_playback_active(True)
    for n in range(30):
        outcome=c.accept_microphone_frame(frame(n))
        assert outcome.suppressed_echo_risk
        assert not outcome.interrupt_requested
    assert c.session.status is StreamStatus.LISTENING
    assert not c.cancellation.is_set()
    with pytest.raises(RuntimeError,match="no accepted speech"):
        c.interrupt_command()
    c.set_playback_active(False)
    for n in range(30,33):
        assert c.accept_microphone_frame(frame(n)).suppressed_echo_risk
    # The forwarded sequence is compact, despite 33 suppressed external frames.
    for n in range(33,36):
        result=c.accept_microphone_frame(frame(n))
    assert result.speech_started and not result.interrupt_requested
    assert c.session.status is StreamStatus.RECORDING


def test_verified_aec_allows_early_barge_in_before_asr_final():
    c=controller(EchoPolicy.VERIFIED_AEC)
    c.set_playback_active(True)
    events=[c.accept_microphone_frame(frame(n)) for n in range(3)]
    assert [x.interrupt_requested for x in events]==[False,False,True]
    assert c.cancellation.is_set()
    assert c.barge_in_detected
    assert events[-1].decode_request is None
    command=c.interrupt_command()
    assert command.reason=="USER_SPEECH"
    assert command.conversation_id==c.session.source.conversation_id
    assert command.utterance_id==c.session.utterance_id
    assert command.input_channel_id==c.session.source.channel_id
    assert command.output_channel_id==c.target.channel_id
    with pytest.raises(RuntimeError,match="must never restart"):
        c.set_playback_active(True)
    c.set_playback_active(False)
    result=None
    for n in range(3,8):
        result=c.accept_microphone_frame(frame(n,SILENCE))
    assert result.decode_request is not None
    assert result.decode_request.status=="FINAL"
    assert c.session.status is StreamStatus.FINAL_PENDING


def test_speech_without_playback_never_interrupts_response():
    c=controller(EchoPolicy.VERIFIED_AEC)
    for n in range(3):
        event=c.accept_microphone_frame(frame(n))
    assert event.speech_started and not event.interrupt_requested
    assert not c.cancellation.is_set()



@pytest.mark.parametrize("mode", [
    EchoPolicy.VERIFIED_AEC,
    EchoPolicy.BLOCK_MIC_DURING_OUTPUT,
])
def test_speech_accepted_before_first_clip_prevents_playback_start(mode):
    c = controller(mode)
    for n in range(3):
        observed = c.accept_microphone_frame(frame(n))
    assert observed.speech_started
    assert c.session.status is StreamStatus.RECORDING
    assert c.set_playback_active(True) is True
    assert c.barge_in_detected and c.cancellation.is_set()
    command = c.interrupt_command()
    assert command.reason == "USER_SPEECH"
    assert command.utterance_id == c.session.utterance_id
    assert c._playback_active is False  # attempted start was refused
    with pytest.raises(RuntimeError, match="must never restart"):
        c.set_playback_active(True)
    # The utterance must remain eligible for ASR; no guard may discard it.
    for n in range(3, 8):
        result = c.accept_microphone_frame(frame(n, SILENCE))
    assert result.decode_request is not None
    assert result.decode_request.status == "FINAL"


def test_speech_in_interclip_gap_blocks_playback_resume():
    c = controller(EchoPolicy.VERIFIED_AEC, guard=0)
    assert c.set_playback_active(True) is False
    assert c.set_playback_active(False) is False
    for n in range(3):
        c.accept_microphone_frame(frame(n))
    assert c.session.status is StreamStatus.RECORDING
    assert c.set_playback_active(True) is True
    assert c.cancellation.is_set()
    assert c._playback_active is False
    with pytest.raises(RuntimeError, match="must never restart"):
        c.set_playback_active(True)


def test_final_pending_user_utterance_blocks_output_restart():
    c = controller(EchoPolicy.VERIFIED_AEC)
    for n in range(3):
        c.accept_microphone_frame(frame(n))
    request = c.session.finish()
    assert request is not None and request.status == "FINAL"
    assert c.session.status is StreamStatus.FINAL_PENDING
    assert c.set_playback_active(True) is True
    assert c.interrupt_command().utterance_id == c.session.utterance_id


def test_frame_source_order_and_timestamp_are_checked_after_suppression():
    c=controller()
    c.set_playback_active(True)
    c.accept_microphone_frame(frame(0))
    with pytest.raises(ValueError,match="gap"):
        c.accept_microphone_frame(frame(2))
    with pytest.raises(ValueError,match="duplicate"):
        c.accept_microphone_frame(frame(0))
    c.accept_microphone_frame(frame(1))
    with pytest.raises(ValueError,match="backward"):
        c.accept_microphone_frame(Pcm16Frame(
            sequence=2,pcm16=SPEECH,captured_at=CAPTURE,
        ))


def test_manual_stop_does_not_cancel_microphone_or_other_tasks():
    c=controller(EchoPolicy.VERIFIED_AEC)
    c.set_playback_active(True)
    c.stop()
    assert c.cancellation.is_set()
    assert c.session.status is StreamStatus.LISTENING
    for n in range(3):
        assert c.accept_microphone_frame(frame(n)).suppressed_echo_risk
    assert c.session.status is StreamStatus.LISTENING
    with pytest.raises(RuntimeError,match="no accepted speech"):
        c.interrupt_command()
