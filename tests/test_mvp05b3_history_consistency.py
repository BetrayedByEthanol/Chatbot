"""MVP-05B.3: immediate report precedence and historical commit consistency."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

psycopg=pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus

from persona_agent.conversation import (
    ingest_text,prepare_text_context,commit_text_response,respond_to_text,
    acknowledge_text_delivery,lookup_response,
)
from persona_agent.conversation_contracts import (
    ModelReply,StaleHistoricalContext,TextInputCommand,
)
from persona_agent.memory import admit_memory,amend_memory,search_memories
from persona_agent.memory_retrieval import search_relevant_history
from test_phase2_memory import actor,call,seed,turn,candidate,correction

QUERY="Do you remember my favorite drink?"
NEW="Actually my favorite drink is coffee."


def _saved(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,"My favorite drink is tea.")
    saved=call(dsn,admit_memory,actor(user),candidate(instance,conversation,source))
    return user,instance,source,saved


def _revision(dsn,user,instance):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(
            """SELECT revision FROM historical_recall_revisions
               WHERE user_id=%s AND instance_id=%s""",(user,instance),
        ).fetchone()[0]


def _approve(dsn,user,instance,source,saved,*,version=1,value="coffee",quote=NEW):
    return call(dsn,amend_memory,actor(user),correction(
        instance,source.conversation_id,source,saved.memory_id,
        version=version,value=value,quote=quote,
    ))


def test_ingress_report_suppresses_old_value_before_worker_without_amending(dsn):
    user,instance,original,saved=_saved(dsn)
    assert call(dsn,search_memories,actor(user),instance,"favorite drink")[0].value_text=="tea"
    cmd=TextInputCommand(instance_id=instance,conversation_id=uuid4(),
        expected_generation=1,idempotency_key="newer-drink",text=NEW)
    before=_revision(dsn,user,instance)
    newer=call(dsn,ingest_text,actor(user),cmd)
    assert _revision(dsn,user,instance)>before
    after=_revision(dsn,user,instance)
    assert call(dsn,ingest_text,actor(user),cmd).replayed
    assert _revision(dsn,user,instance)==after
    assert call(dsn,search_memories,actor(user),instance,"favorite drink")==()
    assert call(dsn,search_relevant_history,actor(user),instance,"favorite drink")==()
    request=turn(dsn,user,instance,uuid4(),QUERY)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert "unresolved. Do not assert the old value" in ctx.messages[0].content
    assert "favorite_drink: tea" not in ctx.messages[0].content
    assert "favorite_drink: coffee" not in ctx.messages[0].content
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT source_event_id,predicate_key,value_text
               FROM explicit_memory_reports WHERE source_turn_id=%s""",(newer.turn_id,),
        ).fetchone()==(newer.event_id,"favorite_drink","coffee")
        assert conn.execute(
            "SELECT value_text,version FROM memory_records WHERE memory_id=%s",
            (saved.memory_id,),
        ).fetchone()==("tea",1)
        assert conn.execute(
            """SELECT count(*) FROM memory_extraction_candidates
               WHERE user_id=%s AND instance_id=%s""",(user,instance),
        ).fetchone()[0]==0
    changed=_approve(dsn,user,instance,newer,saved)
    assert changed.version==2
    hits=call(dsn,search_memories,actor(user),instance,"favorite drink")
    assert [(h.value_text,h.version,h.source_turn_id) for h in hits]==[
        ("coffee",2,newer.turn_id)
    ]


@pytest.mark.parametrize("text",[
    "Maybe my favorite drink is coffee.",
    '"My favorite drink is coffee."',
    "What if my favorite drink is coffee?",
    "My favorite drink is coffee and tea.",
    "My favorite drink is something secret.",
    "I prefer coffee over tea.",  # different canonical slot
])
def test_unsupported_or_different_slot_report_does_not_suppress_saved_fact(dsn,text):
    user,instance,_,saved=_saved(dsn)
    source=turn(dsn,user,instance,uuid4(),text)
    hits=call(dsn,search_memories,actor(user),instance,"favorite drink")
    assert [(h.memory_id,h.value_text) for h in hits]==[(saved.memory_id,"tea")]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM explicit_memory_reports
               WHERE source_turn_id=%s AND predicate_key='favorite_drink'""",
            (source.turn_id,),
        ).fetchone()[0]==0


def test_unowned_older_raw_report_does_not_reappear_in_cross_session_recall(dsn):
    user,instance,_=seed(dsn)
    from test_phase1_conversation import FakeGateway
    old=turn(dsn,user,instance,uuid4(),"My favorite drink is tea.")
    new=turn(dsn,user,instance,uuid4(),NEW)
    for source in (old,new):
        response=call(dsn,respond_to_text,actor(user),source.turn_id,FakeGateway("Understood."))
        call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    request=turn(dsn,user,instance,uuid4(),QUERY)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    prompt=ctx.messages[0].content
    assert "Prior-session user statements" in prompt
    assert NEW in prompt and str(new.turn_id) in prompt
    assert "My favorite drink is tea." not in prompt and str(old.turn_id) not in prompt
    assert call(dsn,search_memories,actor(user),instance,"favorite drink")==()


@pytest.mark.parametrize("change",["REPORT","APPROVAL","PRIVACY","CANDIDATE","NEW_SLOT"])
def test_stale_historical_model_context_is_rejected_and_turn_remains_pending(dsn,change):
    user,instance,_,saved=_saved(dsn)
    # A separate session can change evidence without changing the current
    # conversation revision. The old conversation fence alone is insufficient.
    request=turn(dsn,user,instance,uuid4(),QUERY)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.historical_fence is not None and "favorite_drink: tea" in ctx.messages[0].content
    if change=="REPORT":
        turn(dsn,user,instance,uuid4(),NEW)
    elif change=="APPROVAL":
        # Deliberately unsupported automatic grammar: isolate the Memory
        # revision trigger rather than relying on an ingress report bump.
        text="Correction: coffee is my favorite drink."
        evidence=turn(dsn,user,instance,uuid4(),text)
        _approve(dsn,user,instance,evidence,saved,quote=text)
    elif change=="PRIVACY":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(
                "UPDATE memory_records SET privacy_class='SENSITIVE' WHERE memory_id=%s",
                (saved.memory_id,),
            )
    elif change=="CANDIDATE":
        text="Correction: coffee is my favorite drink."
        evidence=turn(dsn,user,instance,uuid4(),text)
        from test_phase2_retrieval import _conflict
        _conflict(dsn,user,instance,evidence,saved,kind="PREFERENCE",
                  predicate="favorite_drink",value="coffee",text=text)
    else:
        text="I have a favorite garden plant called sage."
        evidence=turn(dsn,user,instance,uuid4(),text)
        call(dsn,admit_memory,actor(user),candidate(
            instance,evidence.conversation_id,evidence,predicate="favorite_garden_plant",
            value="sage",quote=text,
        ))
    with pytest.raises(StaleHistoricalContext,match="historical evidence changed"):
        call(dsn,commit_text_response,actor(user),ctx,
             ModelReply(model_id="old-context",text="Your favorite drink is tea."),
             model_generated=True)
    assert call(dsn,lookup_response,actor(user),request.turn_id) is None
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT status FROM conversation_turns WHERE turn_id=%s",
                            (request.turn_id,)).fetchone()==("PENDING",)
    fresh=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert fresh.historical_fence.revision>ctx.historical_fence.revision


def test_model_retries_once_using_new_owner_revision_without_locks(dsn):
    user,instance,_,saved=_saved(dsn)
    request=turn(dsn,user,instance,uuid4(),QUERY)
    with psycopg.connect(dsn,autocommit=True) as conn:
        class CorrectingGateway:
            calls=0
            def generate(self,messages):
                assert conn.info.transaction_status==TransactionStatus.IDLE
                self.calls+=1
                if self.calls==1:
                    assert "favorite_drink: tea" in messages[0].content
                    new=turn(dsn,user,instance,uuid4(),NEW)
                    _approve(dsn,user,instance,new,saved)
                    return ModelReply(model_id="stale-test",text="Tea.")
                assert "favorite_drink: coffee" in messages[0].content
                assert "favorite_drink: tea" not in messages[0].content
                return ModelReply(model_id="fresh-test",text="Coffee.")
        gateway=CorrectingGateway()
        reply=respond_to_text(conn,actor(user),request.turn_id,gateway)
        assert gateway.calls==2 and reply.text=="Coffee."
        assert reply.model_id=="fresh-test"
        replay=respond_to_text(conn,actor(user),request.turn_id,gateway)
        assert replay.response_id==reply.response_id and gateway.calls==2
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses WHERE turn_id=%s",
            (request.turn_id,),
        ).fetchone()[0]==1


def test_repeated_changes_exhaust_one_retry_without_persisting_stale_reply(dsn):
    user,instance,_,saved=_saved(dsn)
    request=turn(dsn,user,instance,uuid4(),QUERY)
    class ContinuallyChanging:
        calls=0
        def generate(self,messages):
            self.calls+=1
            value="coffee" if self.calls==1 else "water"
            turn(dsn,user,instance,uuid4(),f"My favorite drink is {value}.")
            return ModelReply(model_id="stale-test",text="Tea.")
    gateway=ContinuallyChanging()
    with pytest.raises(StaleHistoricalContext):
        call(dsn,respond_to_text,actor(user),request.turn_id,gateway)
    assert gateway.calls==2
    assert call(dsn,lookup_response,actor(user),request.turn_id) is None


def test_unrelated_scope_and_ordinary_turn_do_not_invalidate_response(dsn,monkeypatch):
    user,instance,_,saved=_saved(dsn)
    request=turn(dsn,user,instance,uuid4(),QUERY)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    stranger,other,_=seed(dsn)
    turn(dsn,stranger,other,uuid4(),NEW)
    assert _revision(dsn,user,instance)==ctx.historical_fence.revision
    reply=call(dsn,commit_text_response,actor(user),ctx,
               ModelReply(model_id="scope-test",text="Tea."),model_generated=True)
    assert reply.text=="Tea."
    ordinary=turn(dsn,user,instance,uuid4(),"Explain TCP sockets.")
    def forbidden(*args,**kwargs):
        raise AssertionError("ordinary context must not query historical revision")
    monkeypatch.setattr("persona_agent.memory_read_policy.capture_historical_read_fence",forbidden)
    ordinary_ctx=call(dsn,prepare_text_context,actor(user),ordinary.turn_id)
    assert ordinary_ctx.historical_fence is None
    turn(dsn,user,instance,uuid4(),NEW)
    reply=call(dsn,commit_text_response,actor(user),ordinary_ctx,
               ModelReply(model_id="scope-test",text="TCP is a transport protocol."),
               model_generated=True)
    assert "TCP" in reply.text


def test_rollback_does_not_advance_historical_revision(dsn):
    user,instance,_,saved=_saved(dsn)
    before=_revision(dsn,user,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(RuntimeError):
            with conn.transaction():
                conn.execute(
                    "UPDATE memory_records SET privacy_class='SENSITIVE' WHERE memory_id=%s",
                    (saved.memory_id,),
                )
                raise RuntimeError("rollback test")
    assert _revision(dsn,user,instance)==before
    assert call(dsn,search_memories,actor(user),instance,"favorite drink")[0].value_text=="tea"


def test_expiry_changes_eligibility_without_revision_write(dsn):
    user,instance,_,saved=_saved(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        expiry=conn.execute(
            """UPDATE memory_records SET expires_at=clock_timestamp()+interval '3 seconds'
               WHERE memory_id=%s RETURNING expires_at""",(saved.memory_id,),
        ).fetchone()[0]
    request=turn(dsn,user,instance,uuid4(),QUERY)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.historical_fence.next_expiry==expiry
    assert "favorite_drink: tea" in ctx.messages[0].content
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM
               (%s::timestamptz-clock_timestamp())))+0.02)""",(expiry,),
        )
    assert _revision(dsn,user,instance)==ctx.historical_fence.revision
    with pytest.raises(StaleHistoricalContext,match="expired"):
        call(dsn,commit_text_response,actor(user),ctx,
             ModelReply(model_id="expired-test",text="Tea."),model_generated=True)
    assert call(dsn,lookup_response,actor(user),request.turn_id) is None
