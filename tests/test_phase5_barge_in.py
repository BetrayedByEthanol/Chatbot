"""P5-04 PostgreSQL: durable TTS stop fences, races, replay and provenance."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.lifecycle import switch_persona,SwitchPersonaCommand
from persona_agent.runtime import PermissionDenied,StaleSelection
from persona_agent.speech_interruptions import interrupt_speech_response
from persona_agent.speech_output_contracts import (
    PlaybackAck,SpeechDeliveryConflict,SpeechOutputTarget,
)
from persona_agent.speech_output_state import (
    acknowledge_speech_clip,prepare_speech_clip,stream_response_speech,
)
from persona_agent.turn_taking_contracts import SpeechInterruptCommand
from test_phase1_conversation import actor,call,seed
from test_phase5_speech_output import (
    ack,clip_command,response,FakeSynthesizer,
)


def command(dsn, response_id, instance, *, channel="browser:voice",
            generation=1, utterance=None, reason="USER_SPEECH"):
    with psycopg.connect(dsn,autocommit=True) as conn:
        conversation_id=conn.execute(
            "SELECT conversation_id FROM conversation_responses WHERE response_id=%s",
            (response_id,),
        ).fetchone()[0]
    return SpeechInterruptCommand(
        response_id=response_id,conversation_id=conversation_id,instance_id=instance,
        expected_generation=generation,output_channel_id=channel,
        input_channel_id="authenticated-microphone",
        reason=reason,
        utterance_id=(utterance or uuid4()) if reason=="USER_SPEECH" else None,
    )


def test_stop_preserves_played_clip_and_fences_unplayed_clips(dsn):
    user,instance,reply=response(dsn,text="One. Two. Three.")
    clips=[
        call(dsn,prepare_speech_clip,actor(user),clip_command(
            reply.response_id,index=i,count=3,
        ))
        for i in range(3)
    ]
    for status in ("QUEUED","STARTED","PLAYED"):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clips[0],status))
    call(dsn,acknowledge_speech_clip,actor(user),ack(clips[1],"QUEUED"))
    call(dsn,acknowledge_speech_clip,actor(user),ack(clips[1],"STARTED"))
    stopped=command(dsn,reply.response_id,instance)
    result=call(dsn,interrupt_speech_response,actor(user),stopped)
    assert result.interrupted_clips==2 and not result.replayed
    repeated=call(dsn,interrupt_speech_response,actor(user),stopped)
    assert repeated.replayed and repeated.interruption_id==result.interruption_id
    assert repeated.interrupted_clips == 2
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM speech_clips WHERE response_id=%s ORDER BY clip_index",
            (reply.response_id,),
        ).fetchall()==[("PLAYED",),("INTERRUPTED",),("INTERRUPTED",)]
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='communication.speech_output.interrupted.v1'
                 AND payload->>'response_id'=%s""",
            (str(reply.response_id),),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='communication.speech_clip.interrupted.v1'
                 AND payload->>'response_id'=%s""",
            (str(reply.response_id),),
        ).fetchone()[0]==2
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,prepare_speech_clip,actor(user),clip_command(
            reply.response_id,index=0,count=3,
        ))
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clips[1],"PLAYED"))


def test_barrier_fences_inflight_tts_even_if_host_cannot_cancel_provider(dsn):
    user,instance,reply=response(dsn,text="Short.")
    cmd=command(dsn,reply.response_id,instance)
    class DelayedTTS(FakeSynthesizer):
        def synthesize_wav(self,text):
            call(dsn,interrupt_speech_response,actor(user),cmd)
            return super().synthesize_wav(text)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(SpeechDeliveryConflict,match="interrupted"):
            list(stream_response_speech(
                conn,actor(user),reply.response_id,
                SpeechOutputTarget(channel_id="browser:voice"),DelayedTTS(),
            ))
        assert conn.execute(
            "SELECT count(*) FROM speech_clips WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]==0


def test_host_cancellation_avoids_late_clip_generation(dsn):
    user,instance,reply=response(dsn,text="Short.")
    state={"stopped":False}
    class CancellingTTS(FakeSynthesizer):
        def synthesize_wav(self,text):
            state["stopped"]=True
            return super().synthesize_wav(text)
    provider=CancellingTTS()
    with psycopg.connect(dsn,autocommit=True) as conn:
        chunks=list(stream_response_speech(
            conn,actor(user),reply.response_id,
            SpeechOutputTarget(channel_id="browser:voice"),provider,
            should_cancel=lambda:state["stopped"],
        ))
        assert chunks==[]
        assert conn.execute(
            "SELECT count(*) FROM speech_clips WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]==0


def test_stop_before_any_audio_persists_barrier(dsn):
    user,instance,reply=response(dsn)
    result=call(dsn,interrupt_speech_response,actor(user),
                command(dsn,reply.response_id,instance))
    assert result.interrupted_clips==0
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))


def test_wrong_persona_channel_and_origin_cannot_stop_playback(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    with pytest.raises(PermissionDenied):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,uuid4()))
    with pytest.raises(PermissionDenied):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,instance,channel="another:channel"))
    with pytest.raises(PermissionDenied):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,instance).model_copy(
                 update={"conversation_id":uuid4()}))
    stranger,_=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,interrupt_speech_response,actor(stranger),
             command(dsn,reply.response_id,instance))
    valid=command(dsn,reply.response_id,instance)
    call(dsn,interrupt_speech_response,actor(user),valid)
    with pytest.raises(SpeechDeliveryConflict):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,instance))
    assert clip.status=="GENERATED"


def test_old_selected_persona_cannot_interrupt_new_generation(dsn):
    user,instance,other,reply=response(dsn,two=True)
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4())))
    with pytest.raises(StaleSelection):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,instance))


def test_delivery_before_interruption_cannot_be_revoked(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    for status in ("QUEUED","STARTED","PLAYED"):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,status))
    with pytest.raises(SpeechDeliveryConflict,match="already delivered"):
        call(dsn,interrupt_speech_response,actor(user),
             command(dsn,reply.response_id,instance))


def test_live_barge_in_produces_one_user_turn_without_marking_tts_heard(dsn):
    """Early speech stop then the existing ASR FINAL -> Conversation ingress."""
    from datetime import datetime,timedelta,timezone
    from struct import pack
    from persona_agent.multimodal_contracts import VoiceInputSource
    from persona_agent.streaming_asr import (
        Pcm16Frame,StreamConfig,VoiceStreamSession,FRAME_BYTES,
    )
    from persona_agent.turn_taking import EchoPolicy,TurnTakingController
    from persona_agent.conversation import prepare_text_context

    user,instance,reply=response(dsn,text="Assistant is speaking.")
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    for state in ("QUEUED","STARTED"):
        call(dsn,acknowledge_speech_clip,actor(user),ack(clip,state))
    with psycopg.connect(dsn,autocommit=True) as conn:
        conversation_id=conn.execute(
            "SELECT conversation_id FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]
    source=VoiceInputSource(
        conversation_id=conversation_id,instance_id=instance,
        expected_generation=1,channel_id="authenticated-microphone",
    )
    session=VoiceStreamSession(
        actor(user),source,uuid4(),
        StreamConfig(activation_frames=3,preroll_frames=3,
                     endpoint_silence_frames=5,max_idle_frames=100,
                     max_utterance_frames=100),
    )
    control=TurnTakingController(
        session,reply.response_id,SpeechOutputTarget(channel_id="browser:voice"),
        echo_policy=EchoPolicy.VERIFIED_AEC,
    )
    control.set_playback_active(True)
    voiced=pack("<h",3000)*(FRAME_BYTES//2)
    start=datetime(2026,11,13,12,0,tzinfo=timezone.utc)
    def pcm(n,samples):
        return Pcm16Frame(
            sequence=n,pcm16=samples,
            captured_at=start+timedelta(milliseconds=20*n),
        )
    for n in range(3):
        result=control.accept_microphone_frame(pcm(n,voiced))
    assert result.interrupt_requested
    assert control.cancellation.is_set()
    control.set_playback_active(False)
    stopped=call(dsn,interrupt_speech_response,actor(user),control.interrupt_command())
    assert stopped.interrupted_clips==1
    for n in range(3,8):
        result=control.accept_microphone_frame(pcm(n,bytes(FRAME_BYTES)))
    assert result.decode_request and result.decode_request.status=="FINAL"
    with psycopg.connect(dsn,autocommit=True) as conn:
        done=session.apply_decode(conn,result.decode_request,"I have another question")
    assert done.disposition=="COMMITTED"
    # The original stop ACK may arrive late, after FINAL advanced dialogue.
    replayed=call(dsn,interrupt_speech_response,actor(user),control.interrupt_command())
    assert replayed.replayed
    assert replayed.interruption_id==stopped.interruption_id
    assert replayed.interrupted_clips==1
    context=call(dsn,prepare_text_context,actor(user),done.committed.turn_id)
    roles=[(m.role,m.content) for m in context.messages]
    assert ("user","I have another question") in roles
    assert ("assistant","Assistant is speaking.") not in roles
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()==("GENERATED",)
        assert conn.execute(
            """SELECT count(*) FROM conversation_turns
               WHERE conversation_id=%s""",
            (conversation_id,),
        ).fetchone()[0]==2



def test_interruption_replays_after_final_user_transcript_supersedes_reply(dsn):
    user, instance, reply = response(dsn)
    clip = call(dsn, prepare_speech_clip, actor(user), clip_command(reply.response_id))
    stop = command(dsn, reply.response_id, instance)
    first = call(dsn, interrupt_speech_response, actor(user), stop)
    assert first.interrupted_clips == 1
    # Exactly the same transition as an ASR FINAL: a newer user turn is
    # committed after the stop, before the transport receives its ACK.
    turn = call(dsn, ingest_text, actor(user), TextInputCommand(
        conversation_id=stop.conversation_id, instance_id=instance,
        expected_generation=1, idempotency_key=str(uuid4()),
        text="Please answer this instead",
    ))
    assert turn.turn_id
    second = call(dsn, interrupt_speech_response, actor(user), stop)
    assert second.replayed
    assert second.interruption_id == first.interruption_id
    assert second.interrupted_clips == 1
    with pytest.raises(SpeechDeliveryConflict, match="another origin"):
        call(dsn, interrupt_speech_response, actor(user), stop.model_copy(
            update={"utterance_id": uuid4()},
        ))
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM events WHERE event_type=
               'communication.speech_output.interrupted.v1'
               AND payload->>'response_id'=%s""",
            (str(reply.response_id),),
        ).fetchone()[0] == 1


def test_interruption_replays_after_persona_switch_without_reactivation(dsn):
    user, instance, other, reply = response(dsn, two=True)
    stop = command(dsn, reply.response_id, instance)
    initial = call(dsn, interrupt_speech_response, actor(user), stop)
    call(dsn, switch_persona, actor(user), SwitchPersonaCommand(
        expected_instance_id=instance, target_instance_id=other,
        expected_generation=1, idempotency_key=str(uuid4()),
    ))
    replayed = call(dsn, interrupt_speech_response, actor(user), stop)
    assert replayed.replayed
    assert replayed.interruption_id == initial.interruption_id
    assert replayed.interrupted_clips == initial.interrupted_clips
    with pytest.raises(SpeechDeliveryConflict, match="another origin"):
        call(dsn, interrupt_speech_response, actor(user), stop.model_copy(
            update={"input_channel_id": "unauthorized:microphone"},
        ))
    with psycopg.connect(dsn, autocommit=True) as conn:
        selection=conn.execute(
            "SELECT instance_id,generation FROM persona_selection WHERE user_id=%s",
            (user,),
        ).fetchone()
        assert selection == (other, 2)
        assert conn.execute(
            "SELECT count(*) FROM speech_output_interruptions WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0] == 1


def test_competing_stop_commands_create_only_one_barrier(dsn):
    user,instance,reply=response(dsn)
    clip=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    cmd=command(dsn,reply.response_id,instance)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=[
            pool.submit(call,dsn,interrupt_speech_response,actor(user),cmd)
            for _ in range(2)
        ]
        outcomes=[job.result() for job in pending]
    assert sorted(x.replayed for x in outcomes)==[False,True]
    assert outcomes[0].interruption_id==outcomes[1].interruption_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM speech_output_interruptions WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='communication.speech_clip.interrupted.v1'
                 AND payload->>'clip_id'=%s""",
            (str(clip.clip_id),),
        ).fetchone()[0]==1
