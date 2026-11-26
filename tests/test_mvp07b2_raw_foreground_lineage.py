"""MVP-07B2 raw foreground provenance: real PostgreSQL, no model service."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from persona_agent.conversation import (
    acknowledge_text_delivery,commit_text_response,lookup_response,
    prepare_direct_tool_context,prepare_text_context,respond_to_text,
)
from persona_agent.conversation_contracts import (
    InvalidatedResponse,ModelReply,StaleHistoricalContext,
)
from persona_agent.memory import admit_memory,amend_memory
from persona_agent.memory_read_policy import eligible_summary_turn_sql
from persona_agent.raw_context import OMITTED_ASSISTANT,OMITTED_USER,eligible_turn_ids
from test_phase1_conversation import FakeGateway
from test_phase2_memory import actor,call,candidate,correction,seed,turn


# This manually admitted claim intentionally avoids the broad recall cue.
SOURCE="Tea is my usual drink."
ECHO="Tea is your favorite drink."
REQUEST="Explain basic garden care."
NEUTRAL="I planted a rose."


def delivered(dsn,user,source,text,*,max_turns=12):
    response=call(dsn,respond_to_text,actor(user),source.turn_id,FakeGateway(text),
                  max_turns=max_turns)
    call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    return response


def setup_source(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,SOURCE)
    saved=call(dsn,admit_memory,actor(user),candidate(instance,conv,source,quote=SOURCE))
    reply=delivered(dsn,user,source,ECHO)
    return user,instance,conv,source,saved,reply


def invalidate(dsn,user,instance,saved,change):
    if change=="EXPIRE":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_records SET expires_at=clock_timestamp()-interval '1 second' WHERE memory_id=%s",
                         (saved.memory_id,))
    else:
        text="Actually my favorite drink is coffee." if change=="CORRECT" else "Please retract my favorite drink report."
        newer=turn(dsn,user,instance,uuid4(),text)
        call(dsn,amend_memory,actor(user),correction(
            instance,newer.conversation_id,newer,saved.memory_id,action=change,
            quote=text,
        ))


def messages(context):
    return [(message.role,message.content) for message in context.messages[1:]]


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_raw_history_masks_ineligible_source_and_echo_but_keeps_other_turns(dsn,change):
    user,instance,conv,source,saved,reply=setup_source(dsn)
    independent=turn(dsn,user,instance,conv,NEUTRAL)
    # This response receives only its own source, so it remains independent.
    delivered(dsn,user,independent,"Roses need light.",max_turns=1)
    pending=turn(dsn,user,instance,conv,REQUEST)
    before=call(dsn,prepare_text_context,actor(user),pending.turn_id)
    assert messages(before)==[
        ("user",SOURCE),("assistant",ECHO),("user",NEUTRAL),
        ("assistant","Roses need light."),("user",REQUEST),
    ]
    assert before.historical_fence is None
    invalidate(dsn,user,instance,saved,change)
    with pytest.raises(StaleHistoricalContext,match="raw history"):
        call(dsn,commit_text_response,actor(user),before,
             ModelReply(model_id="stale",text="Tea again."),model_generated=True)
    assert call(dsn,lookup_response,actor(user),pending.turn_id) is None
    fresh=call(dsn,prepare_text_context,actor(user),pending.turn_id)
    assert messages(fresh)==[
        ("user",OMITTED_USER),("assistant",OMITTED_ASSISTANT),("user",NEUTRAL),
        ("assistant","Roses need light."),("user",REQUEST),
    ]
    assert source.turn_id not in fresh.raw_context_fence.source_turn_ids
    assert set(fresh.raw_context_fence.source_turn_ids)=={independent.turn_id,pending.turn_id}
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),source.turn_id)
    # Nothing rewrites the original transcript or historical delivery.
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT input_text FROM conversation_turns WHERE turn_id=%s",
                            (source.turn_id,)).fetchone()==(SOURCE,)
        assert conn.execute("SELECT response_text,delivery_status FROM conversation_responses WHERE response_id=%s",
                            (reply.response_id,)).fetchone()==(ECHO,"DELIVERED")


def test_cross_session_correction_retries_once_without_transaction_over_model(dsn):
    user,instance,conv,source,saved,_=setup_source(dsn)
    pending=turn(dsn,user,instance,conv,REQUEST)
    with psycopg.connect(dsn,autocommit=True) as conn:
        class CorrectDuringInference:
            calls=0
            def generate(self,prompt):
                assert conn.info.transaction_status==TransactionStatus.IDLE
                self.calls+=1
                if self.calls==1:
                    assert SOURCE in [m.content for m in prompt]
                    invalidate(dsn,user,instance,saved,"CORRECT")
                    return ModelReply(model_id="old",text="Tea again.")
                assert SOURCE not in [m.content for m in prompt]
                assert OMITTED_USER in [m.content for m in prompt]
                return ModelReply(model_id="fresh",text="Water roses deeply.")
        gateway=CorrectDuringInference()
        reply=respond_to_text(conn,actor(user),pending.turn_id,gateway)
        assert gateway.calls==2 and reply.model_id=="fresh"
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses WHERE turn_id=%s",
            (pending.turn_id,),
        ).fetchone()==(1,)
        ids=conn.execute(
            "SELECT source_turn_ids FROM conversation_response_raw_lineage WHERE response_id=%s",
            (reply.response_id,),
        ).fetchone()[0]
        assert ids==[pending.turn_id]
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)


def test_transitive_raw_dependencies_survive_window_cutoff_and_fence_output(dsn):
    user,instance,conv,source,saved,_=setup_source(dsn)
    second=turn(dsn,user,instance,conv,NEUTRAL)
    second_reply=delivered(dsn,user,second,"We can discuss the rose over tea.")
    pending=turn(dsn,user,instance,conv,REQUEST)
    ctx=call(dsn,prepare_text_context,actor(user),pending.turn_id,max_turns=2)
    assert messages(ctx)==[
        ("user",NEUTRAL),("assistant",second_reply.text),("user",REQUEST),
    ]
    assert set(ctx.raw_context_fence.source_turn_ids)=={
        source.turn_id,second.turn_id,pending.turn_id,
    }
    unrelated=turn(dsn,user,instance,uuid4(),"I drive a Toyota Camry.")
    call(dsn,admit_memory,actor(user),candidate(
        instance,unrelated.conversation_id,unrelated,kind="FACT",
        predicate="car",value="Toyota Camry",quote="I drive a Toyota Camry.",
    ))
    generated=call(dsn,commit_text_response,actor(user),ctx,
                   ModelReply(model_id="current",text="Consider regular watering."))
    assert call(dsn,lookup_response,actor(user),pending.turn_id).response_id==generated.response_id
    from persona_agent.summary_memory import _read_range
    from persona_agent.cognitive_engine import _range
    job=dict(user_id=user,instance_id=instance,conversation_id=conv,
             expected_generation=1,start_sequence=1,end_sequence=2)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for reader in (_read_range,_range):
                assert reader(cur,job)[-1]["response_text"]==second_reply.text
                assert reader(cur,dict(job,start_sequence=2))[0]["response_text"]==OMITTED_ASSISTANT
    invalidate(dsn,user,instance,saved,"CORRECT")
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),pending.turn_id)
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_text_delivery,actor(user),generated.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT response_lineage_current(%s,%s,%s)",(generated.response_id,user,instance),
        ).fetchone()==(False,)
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (generated.response_id,),
        ).fetchone()==("GENERATED",)
    latest=turn(dsn,user,instance,conv,"What nutrients do roses need?")
    # Retain second turn but not original source. Its raw-only assistant
    # echo still becomes ineligible through the exact inherited lineage.
    fresh=call(dsn,prepare_text_context,actor(user),latest.turn_id,max_turns=3)
    assert messages(fresh)==[
        ("user",NEUTRAL),("assistant",OMITTED_ASSISTANT),("user",REQUEST),
        ("user","What nutrients do roses need?"),
    ]
    assert source.turn_id not in fresh.raw_context_fence.source_turn_ids


def test_disputed_current_input_is_preserved_and_response_is_not_reused(dsn):
    user,instance,conv,source,saved,_=setup_source(dsn)
    new_text="Actually my favorite drink is coffee."
    newer=turn(dsn,user,instance,conv,new_text)
    context=call(dsn,prepare_text_context,actor(user),newer.turn_id)
    assert context.messages[-1].content==new_text
    assert not context.raw_context_fence.reusable
    assert newer.turn_id not in context.raw_context_fence.source_turn_ids
    response=call(dsn,commit_text_response,actor(user),context,
                  ModelReply(model_id="current-report",text="Thanks for correcting that."))
    call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    call(dsn,amend_memory,actor(user),correction(instance,conv,newer,saved.memory_id))
    pending=turn(dsn,user,instance,conv,REQUEST)
    fresh=call(dsn,prepare_text_context,actor(user),pending.turn_id,max_turns=2)
    assert messages(fresh)==[
        ("user",new_text),("assistant",OMITTED_ASSISTANT),("user",REQUEST),
    ]


def test_grounding_cannot_reintroduce_masked_user_statement(dsn):
    user,instance,conv,source,saved,_=setup_source(dsn)
    invalidate(dsn,user,instance,saved,"CORRECT")
    pending=turn(dsn,user,instance,conv,"Summarize this conversation.")
    context=call(dsn,prepare_text_context,actor(user),pending.turn_id,max_context_chars=24000)
    assert all(SOURCE not in m.content and ECHO not in m.content for m in context.messages)
    assert source.turn_id not in context.raw_context_fence.source_turn_ids


def test_sql_raw_source_policy_matches_existing_memory_predicate_and_scope(dsn):
    user,instance,conv,source,saved,_=setup_source(dsn)
    foreign,other,_=seed(dsn)
    outsider=turn(dsn,foreign,other,uuid4(),NEUTRAL)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            definition=cur.execute(
                "SELECT pg_get_functiondef('raw_context_eligible_turns(uuid,uuid,uuid,uuid[])'::regprocedure) AS definition",
            ).fetchone()["definition"]
            assert " ".join(eligible_summary_turn_sql().split()) in " ".join(definition.split())
            assert eligible_turn_ids(cur,user,instance,conv,{source.turn_id,outsider.turn_id})=={source.turn_id}
    invalidate(dsn,user,instance,saved,"CORRECT")
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            assert eligible_turn_ids(cur,user,instance,conv,{source.turn_id,outsider.turn_id})==set()


def test_legacy_raw_unknown_fails_closed_without_removing_transcript(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    first=turn(dsn,user,instance,conv,NEUTRAL)
    context=call(dsn,prepare_direct_tool_context,actor(user),first.turn_id)
    response=call(dsn,commit_text_response,actor(user),context,
                  ModelReply(model_id="legacy-test",text="Historical body."))
    call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """INSERT INTO conversation_response_raw_lineage(
                   response_id,user_id,instance_id,conversation_id,
                   source_turn_ids,reusable,legacy_unbound
               ) VALUES (%s,%s,%s,%s,'{}'::uuid[],false,true)""",
            (response.response_id,user,instance,conv),
        )
    with pytest.raises(InvalidatedResponse,match="no recorded raw source fence"):
        call(dsn,lookup_response,actor(user),first.turn_id)
    pending=turn(dsn,user,instance,conv,REQUEST)
    ctx=call(dsn,prepare_text_context,actor(user),pending.turn_id)
    assert messages(ctx)==[("user",NEUTRAL),("assistant",OMITTED_ASSISTANT),("user",REQUEST)]
