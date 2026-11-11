"""Phase 1 durable conversation acceptance on PostgreSQL 16."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from queue import Queue
import time
from uuid import uuid4

import pytest

psycopg=pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery,commit_text_response,ingest_text,
    lookup_response,prepare_text_context,respond_to_text,
)
from persona_agent.conversation_contracts import (
    ModelReply,ModelUnavailable,StaleConversation,TextInputCommand,
)
from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
from persona_agent.runtime import IdempotencyConflict,PermissionDenied,StaleSelection


def seed(dsn,two=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                         (persona,f"phase1-{persona}"))
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES (%s,%s,%s,'Helpful')",(instance,user,persona))
            if two:
                persona2,other=uuid4(),uuid4()
                conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                             (persona2,f"phase1-{persona2}"))
                conn.execute(
                    "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                    "VALUES (%s,%s,%s,'Alternate')",(other,user,persona2))
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')",(user,instance))
    return (user,instance,other) if two else (user,instance)


def actor(user):
    return TrustedActor(user_id=user,source_id="phase1-authenticated")


def call(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def command(instance,conversation=None,text="Hello",generation=1,key=None):
    return TextInputCommand(
        conversation_id=conversation or uuid4(),instance_id=instance,
        expected_generation=generation,
        idempotency_key=key or f"phase1-{uuid4()}",text=text)


class FakeGateway:
    def __init__(self,text="Hello back"):
        self.text=text
        self.calls=[]
    def generate(self,messages):
        self.calls.append(tuple(messages))
        return ModelReply(model_id="fake-model",text=self.text)


def test_durable_ingress_context_delivery_and_restart(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    c=command(instance,conv,"  Hello there  ")
    entered=call(dsn,ingest_text,actor(user),c)
    duplicate=call(dsn,ingest_text,actor(user),c)
    assert entered.sequence==1 and not entered.replayed
    assert duplicate.replayed and duplicate.turn_id==entered.turn_id
    fake=FakeGateway("Welcome")
    with psycopg.connect(dsn,autocommit=True) as conn:
        class VerifyNoTransaction:
            def generate(self,messages):
                assert conn.info.transaction_status==TransactionStatus.IDLE
                return fake.generate(messages)
        reply=respond_to_text(conn,actor(user),entered.turn_id,VerifyNoTransaction())
    assert reply.delivery_status=="GENERATED"
    assert fake.calls[0][-1].content=="Hello there"
    assert "Helpful" in fake.calls[0][0].content
    delivered=call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    assert delivered.delivery_status=="DELIVERED"
    assert call(dsn,acknowledge_text_delivery,actor(user),reply.response_id).replayed

    next_turn=call(dsn,ingest_text,actor(user),command(instance,conv,"Continue?"))
    assert next_turn.sequence==2
    context=call(dsn,prepare_text_context,actor(user),next_turn.turn_id)
    assert [(m.role,m.content) for m in context.messages[1:]]==[
        ("user","Hello there"),("assistant","Welcome"),("user","Continue?")]
    second=call(dsn,respond_to_text,actor(user),next_turn.turn_id,
                FakeGateway("Certainly"))
    assert call(dsn,respond_to_text,actor(user),next_turn.turn_id,
                FakeGateway("Should not be used")).response_id==second.response_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT next_sequence,revision,last_input_turn_id "
            "FROM conversations WHERE conversation_id=%s",(conv,)
        ).fetchone()==(3,5,next_turn.turn_id)
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='conversation.user_text.received.v1' "
            "AND instance_id=%s",(instance,)
        ).fetchone()[0]==2
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='conversation.response.generated.v1' "
            "AND instance_id=%s",(instance,)
        ).fetchone()[0]==2
        assert conn.execute(
            "SELECT count(*) FROM outbox o JOIN events e ON e.event_id=o.event_id "
            "WHERE e.instance_id=%s AND e.event_type LIKE 'conversation.%%'",
            (instance,)
        ).fetchone()[0]==5
    assert call(dsn,lookup_response,actor(user),entered.turn_id).text=="Welcome"


def test_ingress_idempotency_and_tenant_isolation(dsn):
    user,instance=seed(dsn)
    stranger,stranger_instance=seed(dsn)
    conv=uuid4()
    c=command(instance,conv,"Message",key="fixed")
    first=call(dsn,ingest_text,actor(user),c)
    with pytest.raises(IdempotencyConflict):
        call(dsn,ingest_text,actor(user),c.model_copy(update={"text":"Changed"}))
    with pytest.raises(PermissionDenied):
        call(dsn,ingest_text,actor(stranger),c)
    with pytest.raises(PermissionDenied):
        call(dsn,ingest_text,actor(stranger),command(stranger_instance,conv))
    with pytest.raises(PermissionDenied):
        call(dsn,prepare_text_context,actor(stranger),first.turn_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (conv,)
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM command_receipts "
            "WHERE command_type='conversation.input.text.v1' "
            "AND instance_id=%s",(instance,)
        ).fetchone()[0]==1


def test_new_input_supersedes_inflight_model_without_stale_reply(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=call(dsn,ingest_text,actor(user),command(instance,conv,"First"))
    later=[]
    class ChangedDuringInference:
        def generate(self,messages):
            later.append(call(dsn,ingest_text,actor(user),
                              command(instance,conv,"Correction")))
            return ModelReply(model_id="fake",text="Stale reply")
    with pytest.raises(StaleConversation):
        call(dsn,respond_to_text,actor(user),first.turn_id,
             ChangedDuringInference())
    assert call(dsn,lookup_response,actor(user),first.turn_id) is None
    assert later[0].sequence==2
    answer=call(dsn,respond_to_text,actor(user),later[0].turn_id,
                FakeGateway("Corrected"))
    assert answer.text=="Corrected"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id=%s",
            (first.turn_id,)
        ).fetchone()==("SUPERSEDED",)


def test_timeout_keeps_input_durable_for_retry(dsn):
    user,instance=seed(dsn)
    entered=call(dsn,ingest_text,actor(user),command(instance))
    class Unavailable:
        def generate(self,messages):
            raise ModelUnavailable("timeout")
    with pytest.raises(ModelUnavailable):
        call(dsn,respond_to_text,actor(user),entered.turn_id,Unavailable())
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id=%s",
            (entered.turn_id,)
        ).fetchone()==("PENDING",)
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses WHERE turn_id=%s",
            (entered.turn_id,)
        ).fetchone()[0]==0
    assert call(dsn,respond_to_text,actor(user),entered.turn_id,
                FakeGateway("Recovered")).text=="Recovered"


def test_persona_switch_fences_inflight_response_and_delivery(dsn):
    user,instance,other=seed(dsn,two=True)
    entered=call(dsn,ingest_text,actor(user),command(instance))
    context=call(dsn,prepare_text_context,actor(user),entered.turn_id)
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4())))
    with pytest.raises(StaleSelection):
        call(dsn,commit_text_response,actor(user),context,
             ModelReply(model_id="fake",text="Late"))
    with pytest.raises(StaleSelection):
        call(dsn,prepare_text_context,actor(user),entered.turn_id)
    with pytest.raises(StaleSelection):
        call(dsn,ingest_text,actor(user),command(instance))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id=%s",
            (entered.turn_id,)
        ).fetchone()==("PENDING",)
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses WHERE turn_id=%s",
            (entered.turn_id,)
        ).fetchone()[0]==0


def test_undelivered_assistant_text_is_not_treated_as_heard(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=call(dsn,ingest_text,actor(user),command(instance,conv,"First"))
    result=call(dsn,respond_to_text,actor(user),first.turn_id,
                FakeGateway("Not delivered"))
    second=call(dsn,ingest_text,actor(user),command(instance,conv,"Second"))
    context=call(dsn,prepare_text_context,actor(user),second.turn_id)
    assert [m.role for m in context.messages]==["system","user","user"]
    assert "Not delivered" not in repr(context.messages)
    assert result.delivery_status=="GENERATED"


def test_concurrent_model_commits_produce_one_response_and_event(dsn):
    user,instance=seed(dsn)
    entered=call(dsn,ingest_text,actor(user),command(instance))
    barrier=Barrier(2)
    class Concurrent:
        def generate(self,messages):
            barrier.wait(timeout=10)
            return ModelReply(model_id="race",text="One response")
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures=[
            workers.submit(call,dsn,respond_to_text,actor(user),
                           entered.turn_id,Concurrent())
            for _ in range(2)
        ]
        replies=[f.result(timeout=15) for f in futures]
    assert replies[0].response_id==replies[1].response_id
    assert sum(not x.replayed for x in replies)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses WHERE turn_id=%s",
            (entered.turn_id,)
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM events "
            "WHERE event_type='conversation.response.generated.v1' "
            "AND payload->>'turn_id'=%s",(str(entered.turn_id),)
        ).fetchone()[0]==1


def test_delivery_ack_scoped_and_fenced_after_persona_switch(dsn):
    user,instance,other=seed(dsn,two=True)
    stranger,_=seed(dsn)
    entered=call(dsn,ingest_text,actor(user),command(instance))
    reply=call(dsn,respond_to_text,actor(user),entered.turn_id,FakeGateway())
    with pytest.raises(PermissionDenied):
        call(dsn,acknowledge_text_delivery,actor(stranger),reply.response_id)
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4())))
    with pytest.raises(StaleSelection):
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    assert call(dsn,lookup_response,actor(user),entered.turn_id).delivery_status=="GENERATED"


def test_bounded_context_excludes_other_user_and_preserves_current_input(dsn):
    user,instance=seed(dsn)
    other,other_instance=seed(dsn)
    call(dsn,ingest_text,actor(other),
         command(other_instance,text="Other user's private text"))
    entered=call(dsn,ingest_text,actor(user),
                 command(instance,text="x"*1000))
    context=call(dsn,prepare_text_context,actor(user),entered.turn_id,
                 max_turns=1,max_context_chars=512)
    assert sum(len(m.content) for m in context.messages)<=512
    assert context.messages[-1].role=="user"
    assert "private text" not in repr(context.messages)



def test_generated_response_replay_rejects_switched_persona_but_historical_read_works(dsn):
    user,instance,other=seed(dsn,two=True)
    request=call(dsn,ingest_text,actor(user),command(instance,text="Original"))
    gateway=FakeGateway("Response from original persona")
    generated=call(dsn,respond_to_text,actor(user),request.turn_id,gateway)
    assert generated.delivery_status=="GENERATED"
    assert len(gateway.calls)==1

    # While the same generation owns Foreground, a retry safely returns
    # the exact committed response without invoking the model again.
    retry=call(dsn,respond_to_text,actor(user),request.turn_id,gateway)
    assert retry.replayed and retry.response_id==generated.response_id
    assert len(gateway.calls)==1

    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleSelection):
        call(dsn,respond_to_text,actor(user),request.turn_id,FakeGateway())
    # lookup_response is intentionally a historical, scoped read; it never
    # authorizes sending an undelivered reply to the user.
    historical=call(dsn,lookup_response,actor(user),request.turn_id)
    assert historical is not None
    assert historical.response_id==generated.response_id
    assert historical.delivery_status=="GENERATED"
    assert historical.text=="Response from original persona"

    # Selecting the original persona again must not revive generation 1.
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleSelection):
        call(dsn,respond_to_text,actor(user),request.turn_id,FakeGateway())
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses "
            "WHERE response_id=%s",(generated.response_id,),
        ).fetchone()==("GENERATED",)


def test_generated_response_replay_rejects_newer_user_input(dsn):
    user,instance=seed(dsn)
    conversation=uuid4()
    first=call(dsn,ingest_text,actor(user),
               command(instance,conversation,text="First question"))
    generated=call(dsn,respond_to_text,actor(user),first.turn_id,
                   FakeGateway("Answer to first question"))
    assert generated.delivery_status=="GENERATED"
    call(dsn,ingest_text,actor(user),
         command(instance,conversation,text="Correction to the question"))
    with pytest.raises(StaleConversation):
        call(dsn,respond_to_text,actor(user),first.turn_id,FakeGateway())
    historical=call(dsn,lookup_response,actor(user),first.turn_id)
    assert historical.response_id==generated.response_id
    assert historical.delivery_status=="GENERATED"


def test_concurrent_delivery_ack_callbacks_are_idempotent(dsn):
    user,instance=seed(dsn)
    conversation=uuid4()
    incoming=call(dsn,ingest_text,actor(user),command(instance,conversation))
    generated=call(dsn,respond_to_text,actor(user),incoming.turn_id,
                   FakeGateway("Delivered exactly once"))
    pid_queue=Queue()
    def acknowledge():
        with psycopg.connect(dsn,autocommit=True) as conn:
            pid_queue.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            return acknowledge_text_delivery(
                conn,actor(user),generated.response_id,
            )

    # Hold the conversation lock so BOTH callbacks read the initial
    # GENERATED state and block before either can acknowledge it.
    with ThreadPoolExecutor(max_workers=2) as executor:
        with psycopg.connect(dsn,autocommit=True) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT conversation_id FROM conversations "
                    "WHERE conversation_id=%s FOR UPDATE",
                    (conversation,),
                ).fetchone()
                futures=[executor.submit(acknowledge) for _ in range(2)]
                pids=[pid_queue.get(timeout=5) for _ in futures]
                deadline=time.monotonic()+5
                with psycopg.connect(dsn,autocommit=True) as observer:
                    while time.monotonic()<deadline:
                        waits=[
                            observer.execute(
                                "SELECT wait_event_type FROM pg_stat_activity "
                                "WHERE pid=%s",(pid,),
                            ).fetchone()
                            for pid in pids
                        ]
                        if all(w is not None and w[0]=="Lock" for w in waits):
                            break
                        time.sleep(0.025)
                    else:
                        raise AssertionError(
                            "both callbacks did not reach the conversation lock"
                        )
        results=[future.result(timeout=10) for future in futures]

    assert {r.response_id for r in results}=={generated.response_id}
    assert {r.delivery_status for r in results}=={"DELIVERED"}
    assert sorted(r.replayed for r in results)==[False,True]
    assert call(dsn,acknowledge_text_delivery,actor(user),
                generated.response_id).replayed
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status,delivered_at IS NOT NULL "
            "FROM conversation_responses WHERE response_id=%s",
            (generated.response_id,),
        ).fetchone()==("DELIVERED",True)
        # Ingress and generation increment revision twice; only one of
        # the concurrent callbacks may increment it for delivery.
        assert conn.execute(
            "SELECT revision FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()==(3,)
        assert conn.execute(
            "SELECT count(*) FROM events "
            "WHERE event_type='conversation.response.delivered.v1' "
            "AND payload->>'response_id'=%s",
            (str(generated.response_id),),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM outbox o JOIN events e ON e.event_id=o.event_id "
            "WHERE e.event_type='conversation.response.delivered.v1' "
            "AND e.payload->>'response_id'=%s",
            (str(generated.response_id),),
        ).fetchone()[0]==1
