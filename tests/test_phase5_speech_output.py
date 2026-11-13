"""P5-03 real PostgreSQL clip state, authorization and delivered semantics."""

import io
import wave
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, lookup_response,
    prepare_text_context, respond_to_text,
)
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
from persona_agent.runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection,
)
from persona_agent.speech_output_contracts import (
    PlaybackAck, SpeechClipCommand, SpeechDeliveryConflict,
    SpeechOutputTarget, InvalidSpeechAudio,
)
from persona_agent.speech_output_audio import validate_wav_pcm16
from persona_agent.speech_output_state import (
    acknowledge_speech_clip, prepare_speech_clip, stream_response_speech,
    timeout_stale_speech_clips,
)
from test_phase1_conversation import actor, call, seed, command, FakeGateway


def wav_bytes():
    with io.BytesIO() as output:
        with wave.open(output,"wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\x00\x01"*1600)
        return output.getvalue()


class FakeSynthesizer:
    def __init__(self):
        self.calls=[]
        self.audio=wav_bytes()
    def synthesize_wav(self,text):
        self.calls.append(text)
        return self.audio


def response(dsn,text="First sentence. Second sentence!",two=False):
    values=seed(dsn,two=two)
    user,instance=values[:2]
    turn=call(dsn,ingest_text,actor(user),command(instance,text="User request"))
    result=call(dsn,respond_to_text,actor(user),turn.turn_id,FakeGateway(text))
    return (*values,result)


def ack(clip,status):
    return PlaybackAck(
        clip_id=clip.clip_id,response_id=clip.response_id,
        channel_id=clip.channel_id,playback_token=clip.playback_token,
        status=status,
    )


def clip_command(response_id,index=0,count=1,channel="browser:voice"):
    info=validate_wav_pcm16(wav_bytes())
    return SpeechClipCommand(
        response_id=response_id,clip_index=index,clip_count=count,
        channel_id=channel,sample_rate_hz=info.sample_rate_hz,
        duration_ms=info.duration_ms,payload_sha256=info.payload_sha256,
    )


def test_speech_clips_mark_response_delivered_only_after_all_played(dsn):
    user,instance,reply=response(
        dsn,text="First speech chunk! Second speech chunk?"
    )
    synth=FakeSynthesizer()
    with psycopg.connect(dsn,autocommit=True) as conn:
        clips=list(stream_response_speech(
            conn,actor(user),reply.response_id,SpeechOutputTarget(
                channel_id="browser:voice"),synth))
    assert len(clips)==2
    assert synth.calls==["First speech chunk!","Second speech chunk?"]
    assert all(c.record.status=="GENERATED" for c in clips)
    first,second=(c.record for c in clips)
    assert first.clip_id!=second.clip_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT clip_index,status,channel_id FROM speech_clips
               WHERE response_id=%s ORDER BY clip_index""",
            (reply.response_id,),
        ).fetchall()
        assert rows==[(0,"GENERATED","browser:voice"),
                      (1,"GENERATED","browser:voice")]
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)
    for status in ("QUEUED","STARTED","PLAYED"):
        result=call(dsn,acknowledge_speech_clip,actor(user),ack(first,status))
    assert result.status=="PLAYED" and not result.response_delivered
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)
    for status in ("QUEUED","STARTED","PLAYED"):
        result=call(dsn,acknowledge_speech_clip,actor(user),ack(second,status))
    assert result.response_delivered
    assert call(dsn,acknowledge_speech_clip,actor(user),ack(second,"PLAYED")).replayed
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("DELIVERED",)
        assert conn.execute(
            """SELECT count(*) FROM events WHERE event_type=
               'conversation.response.delivered.v1' AND payload->>'response_id'=%s""",
            (str(reply.response_id),),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events WHERE event_type LIKE
               'communication.speech_clip.%' AND payload->>'response_id'=%s""",
            (str(reply.response_id),),
        ).fetchone()[0]==8
        assert conn.execute(
            """SELECT count(*) FROM outbox o JOIN events e USING(event_id)
               WHERE e.payload->>'response_id'=%s
                 AND e.event_type LIKE 'communication.speech_clip.%'""",
            (str(reply.response_id),),
        ).fetchone()[0]==8
    # The existing context builder now includes the fully-played reply.
    typed=call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=reply.conversation_id if hasattr(reply,"conversation_id")
        else call(dsn,lambda conn:conn.execute(
            "SELECT conversation_id FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,)).fetchone()[0]),
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),text="Continue",
    ))
    context=call(dsn,prepare_text_context,actor(user),typed.turn_id)
    assert any(m.role=="assistant" and m.content=="First speech chunk! Second speech chunk?"
               for m in context.messages)


def test_clip_idempotency_and_scope(dsn):
    user,instance,reply=response(dsn)
    first=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    second=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    assert first.clip_id==second.clip_id and second.replayed
    modified=clip_command(reply.response_id).model_copy(update={"duration_ms":900})
    with pytest.raises(IdempotencyConflict):
        call(dsn,prepare_speech_clip,actor(user),modified)
    stranger,_=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,prepare_speech_clip,actor(stranger),clip_command(reply.response_id))
    with pytest.raises(PermissionDenied):
        call(dsn,acknowledge_speech_clip,actor(stranger),ack(first,"QUEUED"))
    with pytest.raises(PermissionDenied):
        call(dsn,acknowledge_speech_clip,actor(user),
             ack(first,"QUEUED").model_copy(update={"playback_token":uuid4()}))
    with pytest.raises(PermissionDenied):
        call(dsn,acknowledge_speech_clip,actor(user),
             ack(first,"QUEUED").model_copy(update={"channel_id":"other:channel"}))
    assert call(dsn,acknowledge_speech_clip,actor(user),ack(first,"QUEUED")).status=="QUEUED"
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,acknowledge_speech_clip,actor(user),ack(first,"PLAYED"))
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,prepare_speech_clip,actor(user),
             clip_command(reply.response_id,channel="other:channel").model_copy(
                 update={"clip_index":1,"clip_count":2}))


def test_interrupted_playback_is_not_heard(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"QUEUED"))
    call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"STARTED"))
    interrupted=call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"INTERRUPTED"))
    assert interrupted.status=="INTERRUPTED" and not interrupted.response_delivered
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"PLAYED"))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)


def test_late_playback_ack_after_switch_never_restores_heard_history(dsn):
    user,instance,other,reply=response(dsn,two=True)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    for status in ("QUEUED","STARTED"):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,status))
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4())))
    played=call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"PLAYED"))
    assert played.status=="PLAYED" and not played.response_delivered
    with pytest.raises(StaleSelection):
        call(dsn,prepare_speech_clip,actor(user),
             clip_command(reply.response_id,index=1,count=2))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)


def test_new_input_blocks_new_playback_but_accepts_late_failure(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=call(dsn,lambda conn:conn.execute(
            "SELECT conversation_id FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,)).fetchone()[0]),
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),text="Correction",
    ))
    with pytest.raises(StaleSelection):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"QUEUED"))
    result=call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"FAILED"))
    assert result.status=="FAILED" and not result.response_delivered


def test_timeout_unacknowledged_clips_without_persona_selection(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE speech_clips SET updated_at=clock_timestamp()"
            "-interval '10 minutes' WHERE clip_id=%s",(clip.clip_id,),
        )
    assert call(dsn,timeout_stale_speech_clips,max_age_seconds=120)==1
    assert call(dsn,timeout_stale_speech_clips,max_age_seconds=120)==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM speech_clips WHERE clip_id=%s",
            (clip.clip_id,),
        ).fetchone()==("TIMEOUT",)
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)
        assert conn.execute(
            """SELECT count(*) FROM events WHERE event_type=
               'communication.speech_clip.timeout.v1'
               AND payload->>'clip_id'=%s""",
            (str(clip.clip_id),),
        ).fetchone()[0]==1
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,"PLAYED"))


def test_invalid_audio_never_creates_clip_or_delivery(dsn):
    user,instance,reply=response(dsn)
    class BadSynth:
        def synthesize_wav(self,text):
            return b"invalid audio"
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(InvalidSpeechAudio):
            list(stream_response_speech(
                conn,actor(user),reply.response_id,
                SpeechOutputTarget(channel_id="browser:voice"),BadSynth()))
        assert conn.execute(
            "SELECT count(*) FROM speech_clips WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]==0


def test_duplicate_concurrent_playback_ack_one_event(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(call,dsn,acknowledge_speech_clip,actor(user),ack(clip,"QUEUED"))
            for _ in range(2)]
        results=[f.result() for f in fs]
    assert sum(not x.replayed for x in results)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='communication.speech_clip.queued.v1'
                 AND payload->>'clip_id'=%s""",
            (str(clip.clip_id),),
        ).fetchone()[0]==1
