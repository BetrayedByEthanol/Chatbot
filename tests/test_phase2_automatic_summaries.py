"""P2-03 automatic summary admission: real PostgreSQL integration contracts."""

from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.automatic_summaries import (
    AutoSummaryPolicy, register_auto_summary_consumer, run_auto_summary_tick,
)
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply, TextInputCommand
from persona_agent.event_delivery import (
    claim_delivery, dispatch_outbox, reap_expired_claims,
)
from persona_agent.lifecycle import (
    BackgroundReason, EnterBackgroundCommand, SwitchPersonaCommand,
    TrustedPlatformActor, enter_background, switch_persona,
)
from persona_agent.operation_contracts import (
    EffectClass, EnqueueOperation, Origin, Policy, Priority,
)
from persona_agent.operation_scheduler import (
    claim_operation, configure_resource_pool, enqueue_operation,
)
from persona_agent.operation_control import fail_operation
import persona_agent.summary_memory as summary_module
from persona_agent.runtime import PermissionDenied
from persona_agent.summary_contracts import SUMMARY_CAPABILITY, SUMMARY_OPERATION, SUMMARY_RESOURCE
from persona_agent.summary_memory import run_summary, schedule_summary


def call(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def seed(dsn, *, alternate=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    other=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"auto-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Auto Memory')""",(instance,user,persona),
            )
            if alternate:
                new_persona,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (new_persona,f"auto-{new_persona}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Other')""",
                    (other,user,new_persona),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",(user,instance),
            )
    call(dsn,configure_resource_pool,SUMMARY_RESOURCE,
         capacity_units=3,p0_reserved_units=1)
    return user,instance,other


def actor(user):
    return TrustedActor(user_id=user,source_id="auto-test-user")


class FakeReply:
    def generate(self,messages):
        return ModelReply(model_id="automatic-test-model",text="I hear you.")


class FakeSummary:
    def generate(self,messages):
        return ModelReply(
            model_id="automatic-summary-test",
            text='{"summary":"The user discussed daily events.","episodes":[]}',
        )


def delivered_turn(dsn,user,instance,conversation,n):
    input_turn=call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),
        text=f"Message number {n} about gardening and daily routines.",
    ))
    response=call(dsn,respond_to_text,actor(user),input_turn.turn_id,FakeReply())
    assert response.delivery_status=="GENERATED"
    ack=call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    assert ack.delivery_status=="DELIVERED"
    return input_turn


def bootstrap(dsn):
    # Previous tests may leave undispatched P0/P1 events. Finish their
    # fanout before registering this test's unique consumer.
    with psycopg.connect(dsn,autocommit=True) as conn:
        for _ in range(500):
            if dispatch_outbox(conn,limit=1000)==0:
                break
        else:
            raise AssertionError("prior outbox fanout did not quiesce")
        consumer="auto-test-"+uuid4().hex
        register_auto_summary_consumer(conn,consumer_id=consumer)
        return consumer


def tick(dsn,consumer,user,**kwargs):
    return call(
        dsn,run_auto_summary_tick,worker_id="auto-memory-test",
        consumer_id=consumer,reconcile_user_id=user,**kwargs,
    )


def claim_summary(dsn):
    lease=call(
        dsn,claim_operation,worker_id="summary-executor",
        capabilities=(SUMMARY_CAPABILITY,),
        operation_types=(SUMMARY_OPERATION,),
    )
    assert lease is not None
    return lease


def test_delivery_events_automatically_admit_contiguous_summary_ranges(dsn):
    consumer=bootstrap(dsn)
    user,instance,_=seed(dsn)
    conversation=uuid4()
    for n in range(1,10):
        delivered_turn(dsn,user,instance,conversation,n)
    before=tick(dsn,consumer,user)
    assert before.admitted==0

    delivered_turn(dsn,user,instance,conversation,10)
    eligible=tick(dsn,consumer,user)
    assert eligible.admitted==1
    assert tick(dsn,consumer,user).admitted==0

    with psycopg.connect(dsn,autocommit=True) as conn:
        ops=conn.execute(
            """SELECT o.operation_id,o.origin,o.priority_class,o.execution_policy,
                      o.status,j.start_sequence,j.end_sequence
               FROM operations o JOIN conversation_summary_jobs j
                 ON j.operation_id=o.operation_id
               WHERE j.conversation_id=%s""",(conversation,),
        ).fetchall()
    assert len(ops)==1
    assert ops[0][1:]==(
        "COGNITIVE","P1","FOREGROUND_ASYNC","QUEUED",1,8,
    )
    first=call(dsn,run_summary,claim_summary(dsn),FakeSummary())
    assert (first.start_sequence,first.end_sequence)==(1,8)
    assert tick(dsn,consumer,user).admitted==0

    for n in range(11,19):
        delivered_turn(dsn,user,instance,conversation,n)
    again=tick(dsn,consumer,user)
    assert again.admitted==1
    second=call(dsn,run_summary,claim_summary(dsn),FakeSummary())
    assert (second.start_sequence,second.end_sequence)==(9,16)
    assert tick(dsn,consumer,user).admitted==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==16
        assert conn.execute(
            """SELECT count(*) FROM conversation_summary_jobs
               WHERE conversation_id=%s""",(conversation,),
        ).fetchone()[0]==2


def test_lost_ack_after_operation_admission_replays_without_duplicate(dsn):
    consumer=bootstrap(dsn)
    user,instance,_=seed(dsn)
    conversation=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,conversation,n)
    with psycopg.connect(dsn,autocommit=True) as conn:
        for _ in range(80):
            if dispatch_outbox(conn,limit=1000)==0:
                break
    # Simulate crash after a durable P2-02 admission but before its
    # associated event delivery ACK.
    lease=call(
        dsn,claim_delivery,consumer_id=consumer,worker_id="crashed-worker",
    )
    assert lease is not None
    admitted=call(
        dsn,schedule_summary,
        TrustedPlatformActor(user_id=user,source_id="memory.auto_summary.v1"),
        conversation,instance,1,
    )
    assert not admitted.replayed
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE event_deliveries SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE delivery_id=%s",(lease.delivery_id,),
        )
        reap_expired_claims(conn)
    recovered=tick(dsn,consumer,user)
    assert recovered.processed>0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_summary_jobs
               WHERE conversation_id=%s""",(conversation,),
        ).fetchone()[0]==1
    call(dsn,run_summary,claim_summary(dsn),FakeSummary())


def test_inactive_delivery_deferred_then_resumes_after_switch_back(dsn):
    consumer=bootstrap(dsn)
    user,instance,other=seed(dsn,alternate=True)
    conversation=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,conversation,n)

    away=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert away.generation==2
    parked=tick(dsn,consumer,user)
    assert parked.admitted==0 and parked.deferred>0
    back=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    assert back.generation==3
    resumed=tick(dsn,consumer,user)
    assert resumed.admitted==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        generation=conn.execute(
            """SELECT o.selection_generation FROM operations o
               JOIN conversation_summary_jobs j USING(operation_id)
               WHERE j.conversation_id=%s""",(conversation,),
        ).fetchone()[0]
    assert generation==3
    call(dsn,run_summary,claim_summary(dsn),FakeSummary())


def test_background_handover_schedules_short_range_without_model_call(dsn):
    consumer=bootstrap(dsn)
    user,instance,_=seed(dsn)
    conversation=uuid4()
    for n in range(1,5):
        delivered_turn(dsn,user,instance,conversation,n)
    assert tick(dsn,consumer,user).admitted==0
    handoff=call(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,
        expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,
        idempotency_key=str(uuid4()),
    ))
    assert handoff.mode.value=="BACKGROUND"
    scheduled=tick(dsn,consumer,user)
    assert scheduled.admitted==1
    lease=claim_summary(dsn)
    committed=call(dsn,run_summary,lease,FakeSummary())
    assert (committed.start_sequence,committed.end_sequence)==(1,2)


def test_platform_actor_cannot_admit_arbitrary_operations(dsn):
    user,instance,_=seed(dsn)
    platform=TrustedPlatformActor(user_id=user,source_id="auto-test-platform")
    with pytest.raises(PermissionDenied,match="restricted"):
        call(dsn,enqueue_operation,platform,EnqueueOperation(
            operation_id=uuid4(),instance_id=instance,
            expected_generation=1,idempotency_key=str(uuid4()),
            operation_type="test.unauthorized.v1",origin=Origin.COGNITIVE,
            effect_class=EffectClass.NONE,
            execution_policy=Policy.FOREGROUND_ASYNC,
            priority_class=Priority.P1,
            resources={SUMMARY_RESOURCE:1},capabilities=(SUMMARY_CAPABILITY,),
        ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM operations
               WHERE user_id=%s AND instance_id=%s""",(user,instance),
        ).fetchone()[0]==0


def test_reconciliation_skips_blocked_and_already_admitted_ranges(dsn):
    """A one-row scan page must not starve a later eligible conversation."""
    user,instance,_=seed(dsn)
    blocked=uuid4()
    current=call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=blocked,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),
        text="An answer I have not heard yet.",
    ))
    # Its generated response is still deliverable in generation 1.
    pending=call(dsn,respond_to_text,actor(user),current.turn_id,FakeReply())
    assert pending.delivery_status=="GENERATED"
    for n in range(2,11):
        delivered_turn(dsn,user,instance,blocked,n)

    already=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,already,n)
    platform=TrustedPlatformActor(user_id=user,source_id="memory.auto_summary.v1")
    queued=call(dsn,schedule_summary,platform,already,instance,1)
    assert queued is not None and not queued.replayed

    eligible=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,eligible,n)
    # Register AFTER producing and dispatching all three conversations:
    # only reconciliation can discover the eligible one.
    consumer=bootstrap(dsn)
    result=tick(dsn,consumer,user,scan_limit=1)
    assert result.admitted==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        targets=conn.execute(
            """SELECT conversation_id FROM conversation_summary_jobs
               WHERE user_id=%s""",(user,),
        ).fetchall()
    assert {r[0] for r in targets}=={already,eligible}
    for _ in range(2):
        call(dsn,run_summary,claim_summary(dsn),FakeSummary())



def test_reconciliation_handles_temporary_resource_unavailability(dsn):
    """The worker tick must survive rejected enqueue and recover on the next poll."""
    consumer=bootstrap(dsn)
    user,instance,_=seed(dsn)
    conversation=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,conversation,n)
    call(dsn,configure_resource_pool,SUMMARY_RESOURCE,
         capacity_units=1,p0_reserved_units=1)
    try:
        # Event delivery handles the error; the immediate reconciliation
        # must handle the SAME error instead of killing the worker process.
        unavailable=tick(dsn,consumer,user)
        assert unavailable.admitted==0
        assert unavailable.retry_pending>=1
        with psycopg.connect(dsn,autocommit=True) as conn:
            row=conn.execute(
                """SELECT j.operation_id,o.operation_id
                   FROM conversation_summary_jobs j
                   LEFT JOIN operations o ON o.operation_id=j.operation_id
                   WHERE j.conversation_id=%s""",(conversation,),
            ).fetchone()
            assert row is not None and row[1] is None
    finally:
        call(dsn,configure_resource_pool,SUMMARY_RESOURCE,
             capacity_units=3,p0_reserved_units=1)
    # Recover from the staged row without waiting for the delivery retry
    # delay to elapse or requiring another user message.
    restored=tick(dsn,consumer,user)
    assert restored.admitted==1
    assert tick(dsn,consumer,user).admitted==0
    call(dsn,run_summary,claim_summary(dsn),FakeSummary())


def test_summary_admission_replay_keeps_original_origin_for_both_callers(dsn):
    """User and platform replay one operation, never rewrite its fingerprint."""
    user,instance,_=seed(dsn)
    platform=TrustedPlatformActor(user_id=user,source_id="memory.auto_summary.v1")
    for first_actor,next_actor,expected in (
        (platform,actor(user),"COGNITIVE"),
        (actor(user),platform,"USER"),
    ):
        conversation=uuid4()
        for n in range(1,11):
            delivered_turn(dsn,user,instance,conversation,n)
        first=call(
            dsn,schedule_summary,first_actor,conversation,instance,1,
        )
        second=call(
            dsn,schedule_summary,next_actor,conversation,instance,1,
        )
        assert second.operation_id==first.operation_id
        assert second.replayed
        third=call(
            dsn,schedule_summary,first_actor,conversation,instance,1,
        )
        assert third.operation_id==first.operation_id and third.replayed
        with psycopg.connect(dsn,autocommit=True) as conn:
            row=conn.execute(
                """SELECT j.admission_origin,o.origin
                   FROM conversation_summary_jobs j
                   JOIN operations o ON o.operation_id=j.operation_id
                   WHERE j.conversation_id=%s""",(conversation,),
            ).fetchone()
            assert row==(expected,expected)
        call(dsn,run_summary,claim_summary(dsn),FakeSummary())


def test_cross_origin_crash_recovery_racing_producers(dsn,monkeypatch):
    """The origin is durable before enqueue; two callers may safely race to repair."""
    user,instance,_=seed(dsn)
    platform=TrustedPlatformActor(user_id=user,source_id="memory.auto_summary.v1")
    original_enqueue=summary_module.enqueue_operation

    def crashed_enqueue(*args,**kwargs):
        raise RuntimeError("simulated crash after stage, before P0 enqueue")

    for first_actor,expected in ((platform,"COGNITIVE"),(actor(user),"USER")):
        conversation=uuid4()
        for n in range(1,11):
            delivered_turn(dsn,user,instance,conversation,n)
        monkeypatch.setattr(summary_module,"enqueue_operation",crashed_enqueue)
        with pytest.raises(RuntimeError,match="simulated crash"):
            call(dsn,schedule_summary,first_actor,conversation,instance,1)
        monkeypatch.setattr(summary_module,"enqueue_operation",original_enqueue)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(
                call,dsn,schedule_summary,platform,conversation,instance,1,
            )
            two=pool.submit(
                call,dsn,schedule_summary,actor(user),conversation,instance,1,
            )
            receipts=(one.result(),two.result())
        assert receipts[0].operation_id==receipts[1].operation_id
        assert sum(not item.replayed for item in receipts)==1
        with psycopg.connect(dsn,autocommit=True) as conn:
            rows=conn.execute(
                """SELECT j.admission_origin,o.origin
                   FROM conversation_summary_jobs j
                   JOIN operations o ON o.operation_id=j.operation_id
                   WHERE j.conversation_id=%s""",(conversation,),
            ).fetchall()
            assert rows==[(expected,expected)]
        call(dsn,run_summary,claim_summary(dsn),FakeSummary())


def test_reconciliation_recovers_latest_operator_retry_staged_after_failed_job(
    dsn,monkeypatch,
):
    """The newest attempt with no operation must take precedence over an older FAILED."""
    user,instance,_=seed(dsn)
    conversation=uuid4()
    for n in range(1,11):
        delivered_turn(dsn,user,instance,conversation,n)
    platform=TrustedPlatformActor(user_id=user,source_id="memory.auto_summary.v1")
    original=call(dsn,schedule_summary,platform,conversation,instance,1)
    assert original.admission_attempt==1
    for attempt in range(3):
        lease=claim_summary(dsn)
        status=call(
            dsn,fail_operation,lease,reason="temporary test model outage",
            delay_seconds=0,
        )
        assert status.value==("FAILED" if attempt==2 else "RETRY_WAIT")

    original_enqueue=summary_module.enqueue_operation

    def crashed_enqueue(*args,**kwargs):
        raise RuntimeError("operator retry crashed before enqueue")

    monkeypatch.setattr(summary_module,"enqueue_operation",crashed_enqueue)
    with pytest.raises(RuntimeError,match="operator retry crashed"):
        call(
            dsn,schedule_summary,actor(user),conversation,instance,1,
            retry_failed=True,
        )
    monkeypatch.setattr(summary_module,"enqueue_operation",original_enqueue)
    with psycopg.connect(dsn,autocommit=True) as conn:
        staged=conn.execute(
            """SELECT j.admission_attempt,j.admission_origin,o.status
               FROM conversation_summary_jobs j
               LEFT JOIN operations o ON o.operation_id=j.operation_id
               WHERE j.conversation_id=%s ORDER BY j.admission_attempt""",
            (conversation,),
        ).fetchall()
        assert staged==[(1,"COGNITIVE","FAILED"),(2,"USER",None)]

    consumer=bootstrap(dsn)
    recovered=tick(dsn,consumer,user,scan_limit=1)
    assert recovered.admitted==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        restored=conn.execute(
            """SELECT j.admission_attempt,j.admission_origin,o.origin,o.status
               FROM conversation_summary_jobs j
               JOIN operations o ON o.operation_id=j.operation_id
               WHERE j.conversation_id=%s ORDER BY j.admission_attempt""",
            (conversation,),
        ).fetchall()
        assert restored==[
            (1,"COGNITIVE","COGNITIVE","FAILED"),
            (2,"USER","USER","QUEUED"),
        ]
    call(dsn,run_summary,claim_summary(dsn),FakeSummary())
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==8
