"""P2-06 real PostgreSQL and P0 leased-operation integration tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.automatic_cognition import (
    CognitivePolicy,register_cognitive_consumer,run_cognitive_scheduling_tick,
)
from persona_agent.cognitive_contracts import (
    COGNITIVE_OPERATION,COGNITIVE_CAPABILITY,COGNITIVE_RESOURCE,
    CognitiveDraft,CognitiveProposal,InvalidCognitiveProposal,
)
from persona_agent.cognitive_engine import (
    schedule_cognition,prepare_cognition,commit_cognition,run_cognition,
    list_cognitive_proposals,
)
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand,ModelReply
from persona_agent.lifecycle import (
    TrustedPlatformActor,SwitchPersonaCommand,switch_persona,
)
from persona_agent.operation_contracts import (
    EnqueueOperation,Origin,Policy,Priority,EffectClass,StaleOperation,
)
from persona_agent.operation_scheduler import (
    claim_operation,configure_resource_pool,enqueue_operation,
)
from persona_agent.operation_control import reap_stale_operations,fail_operation
from persona_agent.runtime import PermissionDenied
import persona_agent.cognitive_engine as engine_module


def call(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def seed(dsn,*,other=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    another=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"p206-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Reflective')""",(instance,user,persona),
            )
            if other:
                second,another=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (second,f"p206-{second}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Other')""",(another,user,second),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",(user,instance),
            )
    call(dsn,configure_resource_pool,COGNITIVE_RESOURCE,
         capacity_units=3,p0_reserved_units=1)
    return user,instance,another


def actor(user):
    return TrustedActor(user_id=user,source_id="p206-user")


def platform(user):
    return TrustedPlatformActor(user_id=user,source_id="cognition.auto_reflection.v1")


def turns(dsn,user,instance,conversation,n=4):
    rows=[]
    for index in range(n):
        rows.append(call(dsn,ingest_text,actor(user),TextInputCommand(
            conversation_id=conversation,instance_id=instance,
            expected_generation=1,idempotency_key=str(uuid4()),
            text=f"Garden progress discussion number {index}.",
        )))
    return rows


def claim(dsn):
    result=call(dsn,claim_operation,worker_id="cognition-acceptance",
                capabilities=(COGNITIVE_CAPABILITY,),
                operation_types=(COGNITIVE_OPERATION,),lease_seconds=120)
    assert result is not None
    return result


class FixedCognition:
    def __init__(self,*,intention=True,bad_quote=False):
        self.calls=0
        self.intention=intention
        self.bad_quote=bad_quote

    def generate(self,messages):
        self.calls+=1
        items=json.loads(messages[-1].content)
        source=items[0]
        quote="invented evidence" if self.bad_quote else "Garden progress"
        proposals=[{
            "kind":"HYPOTHESIS","subject":"USER",
            "content":"A timing obstacle may be worth asking about.",
            "source_turn_id":source["turn_id"],
            "evidence_quote":quote,"confidence":0.61,
            "trigger_text":None,"expiry_days":None,
        }]
        if self.intention:
            proposals.append({
                "kind":"INTENTION","subject":"CONVERSATION",
                "content":"Ask whether a different garden schedule would help.",
                "source_turn_id":source["turn_id"],
                "evidence_quote":"Garden progress","confidence":0.7,
                "trigger_text":"when garden scheduling is relevant",
                "expiry_days":7,
            })
        return ModelReply(
            model_id="fake-cognitive-model",
            text=json.dumps({"proposals":proposals}),
        )


def test_cognitive_range_commits_evidence_linked_proposals_only(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    history=turns(dsn,user,instance,conversation,4)
    scheduled=call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    assert (scheduled.start_sequence,scheduled.end_sequence)==(1,3)
    lease=claim(dsn)
    gateway=FixedCognition()
    result=call(dsn,run_cognition,lease,gateway)
    assert (result.reflection_count,result.intention_count)==(2,1)
    assert gateway.calls==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==3
        assert conn.execute(
            """SELECT kind,evidence_turn_id,epistemic_status,review_status
               FROM cognitive_reflections WHERE operation_id=%s ORDER BY kind""",
            (lease.operation_id,),
        ).fetchall()==[
            ("HYPOTHESIS",history[0].turn_id,"MODEL_INTERPRETED","PROPOSED"),
            ("INTENTION",history[0].turn_id,"MODEL_INTERPRETED","PROPOSED"),
        ]
        intention=conn.execute(
            """SELECT status,already_asked,delivery_constraints->>
                      'requires_attention_approval',expires_at>clock_timestamp()
               FROM cognitive_intentions WHERE user_id=%s AND instance_id=%s""",
            (user,instance),
        ).fetchone()
        assert intention==("PROPOSED",False,"true",True)
        assert conn.execute(
            "SELECT count(*) FROM memory_records WHERE user_id=%s",(user,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM events WHERE user_id=%s
               AND event_type='cognition.range.reflected.v1'""",
            (user,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (lease.operation_id,),
        ).fetchone()[0]=="COMPLETED"
    exposed=call(dsn,list_cognitive_proposals,actor(user),instance)
    assert len(exposed)==2
    assert all(x["epistemic_status"]=="MODEL_INTERPRETED" for x in exposed)


def test_invalid_model_evidence_never_commits_or_advances_watermark(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    lease=claim(dsn)
    with pytest.raises(InvalidCognitiveProposal,match="evidence"):
        call(dsn,run_cognition,lease,FixedCognition(bad_quote=True))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM cognitive_reflections WHERE user_id=%s",(user,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM cognitive_intentions WHERE user_id=%s",(user,),
        ).fetchone()[0]==0


def test_inference_lease_expiry_rolls_back_all_proposals(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    lease=claim(dsn)
    work=call(dsn,prepare_cognition,lease)
    draft=CognitiveDraft(proposals=(CognitiveProposal(
        kind="OBSERVATION",subject="USER",content="Garden work was mentioned.",
        source_turn_id=work.turns[0]["turn_id"],evidence_quote="Garden",
        confidence=0.7,
    ),))
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(lease.operation_id,),
        )
    with pytest.raises(StaleOperation):
        call(dsn,commit_cognition,lease,work,draft,"fixed-model")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM cognitive_reflections WHERE user_id=%s",(user,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==0
    # Resource claims are global across real PostgreSQL test cases: reaping
    # an intentionally expired lease avoids starving independent P1 suites.
    assert call(dsn,reap_stale_operations,limit=100)>=1


def test_persona_switch_revokes_commit_without_relabeling_as_user_facts(dsn):
    user,instance,other=seed(dsn,other=True)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    lease=claim(dsn)
    work=call(dsn,prepare_cognition,lease)
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    draft=CognitiveDraft(proposals=(CognitiveProposal(
        kind="CONCLUSION",subject="USER",content="Garden effort may be ongoing.",
        source_turn_id=work.turns[0]["turn_id"],evidence_quote="Garden",
        confidence=0.51,
    ),))
    with pytest.raises(StaleOperation):
        call(dsn,commit_cognition,lease,work,draft,"fake-model")
    assert call(dsn,list_cognitive_proposals,actor(user),instance)==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(lease.operation_id,),
        )
    assert call(dsn,reap_stale_operations,limit=100)>=1


def test_staging_failure_replay_and_duplicate_commit_recovery(dsn,monkeypatch):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    original=engine_module.enqueue_operation

    def fail_between_transactions(*args,**kwargs):
        raise RuntimeError("simulated crash after job stage")

    monkeypatch.setattr(engine_module,"enqueue_operation",fail_between_transactions)
    with pytest.raises(RuntimeError,match="simulated crash"):
        call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    monkeypatch.setattr(engine_module,"enqueue_operation",original)
    staged=call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    assert staged.start_sequence==1 and staged.end_sequence==3
    assert call(dsn,schedule_cognition,platform(user),conversation,instance,1) is None
    lease=claim(dsn)
    work=call(dsn,prepare_cognition,lease)
    valid=CognitiveDraft(proposals=(CognitiveProposal(
        kind="HYPOTHESIS",subject="USER",content="Garden timing may matter.",
        source_turn_id=work.turns[0]["turn_id"],
        evidence_quote="Garden progress",confidence=0.5,
    ),))
    first=call(dsn,commit_cognition,lease,work,valid,"fake-model")
    assert first.reflection_count==1 and not first.replayed
    replay_gateway=FixedCognition()

    # Crash after cognitive commit but before P0 operation completion:
    # rerunning with the same valid lease completes without another model call.
    completed=call(dsn,run_cognition,lease,replay_gateway)
    assert completed.replayed
    assert replay_gateway.calls==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM cognitive_reflections WHERE user_id=%s",(user,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM cognitive_jobs WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==1


def test_auto_scheduler_event_and_reconciliation_without_foreground_inference(dsn):
    user,instance,_=seed(dsn)
    consumer="p206-"+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        register_cognitive_consumer(conn,consumer)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    tick=call(
        dsn,run_cognitive_scheduling_tick,worker_id="p206-scheduler",
        consumer_id=consumer,reconcile_user_id=user,
    )
    assert tick.admitted==1
    assert tick.processed>=1
    assert call(
        dsn,run_cognitive_scheduling_tick,worker_id="p206-scheduler",
        consumer_id=consumer,reconcile_user_id=user,
    ).admitted==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM cognitive_jobs WHERE conversation_id=%s""",
            (conversation,),
        ).fetchone()[0]==1
    result=call(dsn,run_cognition,claim(dsn),FixedCognition(intention=False))
    assert result.reflection_count==1


def test_platform_forgery_and_cross_user_review_denied(dsn):
    user,instance,_=seed(dsn)
    foreign,foreign_instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    with pytest.raises(PermissionDenied):
        call(dsn,schedule_cognition,actor(user),conversation,instance,1)
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,platform(user),EnqueueOperation(
            operation_id=uuid4(),instance_id=instance,
            expected_generation=1,idempotency_key="cognition:forged",
            operation_type=COGNITIVE_OPERATION,origin=Origin.COGNITIVE,
            effect_class=EffectClass.NONE,
            execution_policy=Policy.FOREGROUND_ASYNC,
            priority_class=Priority.P1,
            resources={COGNITIVE_RESOURCE:1},
            capabilities=(COGNITIVE_CAPABILITY,),
            concurrency_group=f"cognition:{conversation}",
        ))
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,actor(user),EnqueueOperation(
            operation_id=uuid4(),instance_id=instance,
            expected_generation=1,idempotency_key="user-forged",
            operation_type=COGNITIVE_OPERATION,origin=Origin.COGNITIVE,
            resources={COGNITIVE_RESOURCE:1},
        ))
    call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    call(dsn,run_cognition,claim(dsn),FixedCognition(intention=False))
    assert call(dsn,list_cognitive_proposals,actor(foreign),instance)==()
    assert call(dsn,list_cognitive_proposals,actor(user),foreign_instance)==()


def test_concurrent_admission_has_exactly_one_staged_operation(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    def admit():
        return call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(admit)
        two=pool.submit(admit)
        outcomes=[one.result(),two.result()]
    assert sum(x is not None for x in outcomes)>=1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM cognitive_jobs WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM operations
               WHERE operation_type=%s AND user_id=%s""",
            (COGNITIVE_OPERATION,user),
        ).fetchone()[0]==1



def test_terminal_failure_requires_explicit_operator_retry(dsn):
    """P0 final failure is terminal until a separately authorized fresh attempt."""
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation)
    first=call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    assert first.admission_attempt==1

    for attempt in range(3):
        lease=claim(dsn)
        status=call(
            dsn,fail_operation,lease,reason="operator test outage",
            delay_seconds=0,
        )
        assert status.value==("FAILED" if attempt==2 else "RETRY_WAIT")
    assert call(dsn,schedule_cognition,platform(user),conversation,instance,1) is None
    with pytest.raises(PermissionDenied,match="operator authority"):
        call(dsn,schedule_cognition,platform(user),conversation,instance,1,
             retry_failed=True)
    operator=TrustedPlatformActor(user_id=user,source_id="cognition.operator")
    retried=call(
        dsn,schedule_cognition,operator,conversation,instance,1,
        retry_failed=True,
    )
    assert retried.admission_attempt==2
    assert retried.operation_id!=first.operation_id
    assert (retried.start_sequence,retried.end_sequence)==(
        first.start_sequence,first.end_sequence,
    )
    assert call(dsn,schedule_cognition,operator,conversation,instance,1,
                retry_failed=True) is None
    restored=call(dsn,run_cognition,claim(dsn),FixedCognition(intention=False))
    assert restored.end_sequence==first.end_sequence
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT j.admission_attempt,o.status
               FROM cognitive_jobs j JOIN operations o
                 ON o.operation_id=j.operation_id
               WHERE j.conversation_id=%s
               ORDER BY j.admission_attempt""",(conversation,),
        ).fetchall()
        assert rows==[(1,"FAILED"),(2,"COMPLETED")]


def test_repeated_interpretation_links_independent_user_evidence(dsn):
    """A model repeating itself does not count as its own corroboration."""
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns(dsn,user,instance,conversation,n=4)
    call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    call(dsn,run_cognition,claim(dsn),FixedCognition(intention=False))
    turns(dsn,user,instance,conversation,n=4)
    new=call(dsn,schedule_cognition,platform(user),conversation,instance,1)
    assert new.start_sequence==4
    call(dsn,run_cognition,claim(dsn),FixedCognition(intention=False))
    with psycopg.connect(dsn,autocommit=True) as conn:
        reflections=conn.execute(
            """SELECT reflection_id,related_reflection_id,evidence_turn_id,
                      epistemic_status
               FROM cognitive_reflections
               WHERE user_id=%s AND instance_id=%s ORDER BY created_at""",
            (user,instance),
        ).fetchall()
        assert len(reflections)==2
        assert reflections[0][1] is None
        assert reflections[1][1]==reflections[0][0]
        assert reflections[0][2]!=reflections[1][2]
        assert all(row[3]=="MODEL_INTERPRETED" for row in reflections)
