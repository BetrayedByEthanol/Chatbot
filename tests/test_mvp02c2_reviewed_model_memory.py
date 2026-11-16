"""MVP-02C.2: reviewed async model candidates and conflict-safe recall."""
import json
from uuid import UUID, uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.contracts import TrustedActor
from persona_agent.event_delivery import dispatch_outbox
from persona_agent.conversation import ingest_text, respond_to_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.memory import search_memories
from persona_agent.memory_model_extraction import (
    approve_model_memory_candidate, list_model_memory_candidates,
    register_model_memory_consumer, reject_model_memory_candidate,
    run_model_memory_tick,
)
import persona_agent.memory_model_extraction as model_memory


class OrdinaryConversation:
    def __init__(self):
        self.requests=[]

    def generate(self,messages):
        self.requests.append(tuple(messages))
        return ModelReply(model_id="ordinary.capture",text="I understand.")


class MemoryProposer:
    def __init__(self,connection,*,output="valid"):
        self.connection=connection
        self.output=output
        self.calls=[]

    def generate(self,messages):
        # The P0 handler may ONLY run after inference, in a short DB txn.
        assert self.connection.info.transaction_status==psycopg.pq.TransactionStatus.IDLE
        self.calls.append(tuple(messages))
        input_text=messages[-1].content
        if self.output=="invalid":
            return ModelReply(model_id="memory.mock",text="not json")
        if self.output=="unsafe":
            predicate="enjoys_medication"
        else:
            predicate="enjoys_gardening"
        text=json.dumps({"candidate":{
            "memory_kind":"PREFERENCE","subject_key":"user",
            "predicate_key":predicate,"value_text":"yes",
            "source_quote":input_text,"confidence":0.8,
        }})
        return ModelReply(model_id="memory.mock",text=text)


def call(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def fixture(dsn):
    ordinary=OrdinaryConversation()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),ordinary)
    host.enable_explicit_memory_worker()
    with psycopg.connect(dsn,autocommit=True) as conn:
        for _ in range(30):
            if dispatch_outbox(conn,limit=1000)==0:
                break
        consumer="mvp02c2-"+uuid4().hex
        register_model_memory_consumer(conn,consumer_id=consumer)
    return host,ordinary,consumer


def test_pending_response_parks_without_retry_exhaustion_then_resumes(dsn):
    """A slow foreground response must not permanently block its stream."""
    host,_,consumer=fixture(dsn)
    identity=host.begin_session("slow-foreground")
    actor=TrustedActor(user_id=identity.user_id,source_id="slow-foreground")
    entered=call(
        dsn,ingest_text,actor,TextInputCommand(
            instance_id=identity.instance_id,
            conversation_id=identity.conversation_id,
            expected_generation=identity.generation,
            idempotency_key="slow-first-input",
            text="I've gotten into gardening lately.",
        ),
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        model=MemoryProposer(conn)
        # Even five polling cycles cannot consume the three-attempt
        # failure budget. No model call is permitted before RESPONDED.
        for attempt in range(5):
            outcome=run_model_memory_tick(
                conn,model,consumer_id=consumer,worker_id="slow-worker",
            )
            assert outcome.retry_pending==0
            assert outcome.proposed==0
            assert outcome.deferred==(1 if attempt==0 else 0)
        state=conn.execute(
            """SELECT d.status,d.attempt_count,d.max_attempts
               FROM event_deliveries d JOIN events e ON e.event_id=d.event_id
               WHERE d.consumer_id=%s AND e.event_type=
                     'conversation.user_text.received.v1'
                 AND e.payload->>'turn_id'=%s""",
            (consumer,str(entered.turn_id)),
        ).fetchone()
        assert state[0]=="DEFERRED" and state[1]==1
        assert state[2]<=3
        assert model.calls==[]

    # Finish the original turn through the production conversation owner,
    # not by directly manipulating the delivery or turn status.
    call(dsn,respond_to_text,actor,entered.turn_id,OrdinaryConversation())
    with psycopg.connect(dsn,autocommit=True) as conn:
        model=MemoryProposer(conn)
        resumed=run_model_memory_tick(
            conn,model,consumer_id=consumer,worker_id="slow-worker",
        )
        assert resumed.proposed==1 and resumed.retry_pending==0
        assert len(model.calls)==1
        state=conn.execute(
            """SELECT d.status,d.attempt_count
               FROM event_deliveries d JOIN events e ON e.event_id=d.event_id
               WHERE d.consumer_id=%s AND e.payload->>'turn_id'=%s
                 AND e.event_type='conversation.user_text.received.v1'""",
            (consumer,str(entered.turn_id)),
        ).fetchone()
        assert state==("PROCESSED",2)

    # A later event on the SAME conversation stream is no longer blocked.
    host.send("Maybe I'll try hiking next month.",idempotency_key="later")
    with psycopg.connect(dsn,autocommit=True) as conn:
        continued=run_model_memory_tick(
            conn,MemoryProposer(conn),consumer_id=consumer,
        )
        assert continued.processed==1 and continued.retry_pending==0
    pending=call(
        dsn,list_model_memory_candidates,actor,identity.instance_id,
    )
    assert len(pending)==1 and pending[0].source_turn_id==entered.turn_id


def test_reviewed_model_candidate_requires_authenticated_approval(dsn):
    host,ordinary,consumer=fixture(dsn)
    identity=host.begin_session("source")
    source=host.send("I've gotten into gardening lately.",idempotency_key="source")
    assert host.tick_explicit_memory_worker()["admitted"]==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        model=MemoryProposer(conn)
        tick=run_model_memory_tick(
            conn,model,consumer_id=consumer,worker_id="reviewed-memory",
        )
    assert tick.proposed==1 and tick.retry_pending==0
    assert len(model.calls)==1
    assert call(dsn,search_memories,TrustedActor(
        user_id=identity.user_id,source_id="reviewer",
    ),identity.instance_id,"gardening")==()

    actor=TrustedActor(user_id=identity.user_id,source_id="reviewer")
    pending=call(dsn,list_model_memory_candidates,actor,identity.instance_id)
    assert len(pending)==1
    candidate=pending[0]
    assert candidate.source_turn_id==UUID(source.turn_id)
    assert candidate.source_quote=="I've gotten into gardening lately."
    assert (candidate.memory_kind,candidate.predicate_key,
            candidate.value_text)==("PREFERENCE","enjoys_gardening","yes")
    assert candidate.model_id=="memory.mock"
    approved=call(
        dsn,approve_model_memory_candidate,actor,candidate.candidate_id,
        expected_generation=identity.generation,
    )
    assert approved.version==1 and not approved.replayed
    assert call(
        dsn,approve_model_memory_candidate,actor,candidate.candidate_id,
        expected_generation=identity.generation,
    ).replayed
    assert call(dsn,list_model_memory_candidates,actor,identity.instance_id)==()
    found=call(dsn,search_memories,actor,identity.instance_id,"gardening")
    assert len(found)==1 and found[0].source_turn_id==UUID(source.turn_id)
    assert found[0].value_text=="yes"

    host.begin_session("new")
    host.restart_host()
    host.send(
        "Do you remember what I said about gardening?",
        idempotency_key="recalled",
    )
    prompt=ordinary.requests[-1][0].content
    assert "[PREFERENCE] user.enjoys_gardening: yes" in prompt
    assert source.turn_id in prompt
    assert host.observe_state()["goals"]==0


def test_model_rejection_and_source_isolation(dsn):
    host,_,consumer=fixture(dsn)
    identity=host.begin_session("initial")
    host.send("I've gotten into gardening lately.",idempotency_key="proposal")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert run_model_memory_tick(
            conn,MemoryProposer(conn),consumer_id=consumer,
        ).proposed==1
    actor=TrustedActor(user_id=identity.user_id,source_id="reviewer")
    c=call(dsn,list_model_memory_candidates,actor,identity.instance_id)[0]
    stranger=TrustedActor(user_id=uuid4(),source_id="outsider")
    with pytest.raises(Exception):
        call(dsn,list_model_memory_candidates,stranger,identity.instance_id)
    with pytest.raises(Exception):
        call(dsn,approve_model_memory_candidate,stranger,c.candidate_id,
             expected_generation=identity.generation)
    assert call(dsn,reject_model_memory_candidate,actor,c.candidate_id)
    assert not call(dsn,reject_model_memory_candidate,actor,c.candidate_id)
    with pytest.raises(ValueError):
        call(dsn,approve_model_memory_candidate,actor,c.candidate_id,
             expected_generation=identity.generation)
    assert call(dsn,search_memories,actor,identity.instance_id,"gardening")==()


def test_invalid_model_proposal_is_not_persisted(dsn):
    host,_,consumer=fixture(dsn)
    identity=host.begin_session("initial")
    host.send("I've gotten into gardening lately.",idempotency_key="source")
    with psycopg.connect(dsn,autocommit=True) as conn:
        result=run_model_memory_tick(
            conn,MemoryProposer(conn,output="unsafe"),
            consumer_id=consumer,
        )
    assert result.processed==1 and result.invalid_model_output==1
    assert result.retry_pending==0 and result.proposed==0
    actor=TrustedActor(user_id=identity.user_id,source_id="reviewer")
    assert call(dsn,list_model_memory_candidates,actor,identity.instance_id)==()
    assert call(dsn,search_memories,actor,identity.instance_id,"gardening")==()


def test_approval_and_candidate_decision_roll_back_together(dsn,monkeypatch):
    host,_,consumer=fixture(dsn)
    identity=host.begin_session("initial")
    host.send("I've gotten into gardening lately.",idempotency_key="source")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert run_model_memory_tick(
            conn,MemoryProposer(conn),consumer_id=consumer,
        ).proposed==1
    actor=TrustedActor(user_id=identity.user_id,source_id="reviewer")
    c=call(dsn,list_model_memory_candidates,actor,identity.instance_id)[0]
    original=model_memory._admit_memory_in_transaction

    def interrupted(cur,actor,command):
        original(cur,actor,command)
        raise RuntimeError("interrupt approval before candidate status")
    monkeypatch.setattr(model_memory,"_admit_memory_in_transaction",interrupted)
    with pytest.raises(RuntimeError):
        call(dsn,approve_model_memory_candidate,actor,c.candidate_id,
             expected_generation=identity.generation)
    assert call(dsn,search_memories,actor,identity.instance_id,"gardening")==()
    assert len(call(dsn,list_model_memory_candidates,actor,identity.instance_id))==1
    monkeypatch.setattr(model_memory,"_admit_memory_in_transaction",original)
    result=call(dsn,approve_model_memory_candidate,actor,c.candidate_id,
                expected_generation=identity.generation)
    assert result.version==1 and not result.replayed


def test_newer_unresolved_explicit_conflict_suppresses_stale_memory_recall(dsn):
    host,ordinary,_=fixture(dsn)
    identity=host.begin_session("initial")
    host.send("I enjoy gardening.",idempotency_key="positive")
    host.send("I enjoy reading.",idempotency_key="reading")
    tick=host.tick_explicit_memory_worker()
    assert tick["admitted"]==2
    actor=TrustedActor(user_id=identity.user_id,source_id="reviewer")
    assert len(call(dsn,search_memories,actor,identity.instance_id,"gardening"))==1
    host.send("I don't enjoy gardening anymore.",idempotency_key="contradict")
    tick=host.tick_explicit_memory_worker()
    assert tick["conflicts"]==1
    # The authoritative yes record is NOT overwritten, but it must not be
    # presented as the current preference while a newer contradiction awaits
    # user review. Independent, uncontested memories remain available.
    assert call(dsn,search_memories,actor,identity.instance_id,"gardening")==()
    assert len(call(dsn,search_memories,actor,identity.instance_id,"reading"))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT value_text FROM memory_records
               WHERE user_id=%s AND instance_id=%s
                 AND predicate_key='enjoys_gardening'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]=="yes"

    host.begin_session("later")
    host.restart_host()
    host.send("Do you remember what I said about gardening?",
              idempotency_key="conflicted-recall")
    prompt=ordinary.requests[-1][0].content
    assert "[PREFERENCE] user.enjoys_gardening: yes" not in prompt
    assert "newer user-reported correction" in prompt
    assert "Do not assert the old value" in prompt
    assert host.observe_state()["goals"]==0
