"""P1-02 real PostgreSQL gateway/worker and async-followup acceptance."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery,ingest_text,lookup_response,
)
from persona_agent.conversation_contracts import TextInputCommand,StaleConversation
from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
from persona_agent.operation_contracts import (
    EffectClass,EnqueueOperation,Policy,Priority,ResourceUnavailable,Status,
)
from persona_agent.operation_scheduler import configure_resource_pool,enqueue_operation
from persona_agent.runtime import IdempotencyConflict,PermissionDenied,StaleSelection
from persona_agent.tool_contracts import (
    SLOW_TOOL,RESOURCE_KEY,SlowTextToolCommand,ToolResultNotReady,
)
from persona_agent.tool_gateway import (
    acknowledge_tool_followup,prepare_tool_followup,read_tool_call,
    reconcile_tool_followup,respond_to_text_analysis,respond_with_time,
    run_one_text_tool,schedule_text_analysis,
)


@pytest.fixture(autouse=True)
def isolate_tool_queue(dsn):
    # Shared disposable Phase 0/1 integration DB; avoid leaking runnable work.
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM operation_concurrency_locks")
            conn.execute("DELETE FROM operation_resource_reservations")
            conn.execute(
                "UPDATE execution_resource_pools SET active_units=0,active_non_p0_units=0")
            conn.execute(
                """UPDATE operations SET status='CANCELLED',worker_id=NULL,
                   lease_until=NULL,claimed_at=NULL WHERE status IN (
                   'QUEUED','RUNNING','RETRY_WAIT','CANCEL_REQUESTED',
                   'SUSPEND_REQUESTED','SUSPENDED','BLOCKED')"""
            )


def seed(dsn,two=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"tool-{persona}"))
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES (%s,%s,%s,'Assistant')",(instance,user,persona))
            if two:
                p2,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"tool-{p2}"))
                conn.execute(
                    "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                    "VALUES (%s,%s,%s,'Other')",(other,user,p2))
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')",(user,instance))
    return (user,instance,other) if two else (user,instance)


def actor(user):
    return TrustedActor(user_id=user,source_id="p1-tool-authenticated")


def invoke(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def incoming(dsn,user,instance,text="alpha beta beta gamma",conversation=None):
    return invoke(
        dsn,ingest_text,actor(user),
        TextInputCommand(
            conversation_id=conversation or uuid4(),instance_id=instance,
            expected_generation=1,idempotency_key=f"msg-{uuid4()}",text=text))


def configure_tool_pool(dsn):
    invoke(dsn,configure_resource_pool,RESOURCE_KEY,capacity_units=2)


def finish_initial(dsn,user,response_id):
    return invoke(dsn,acknowledge_text_delivery,actor(user),response_id)


def event_count(dsn,tool_call_id,event_type):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM events WHERE event_type=%s "
            "AND payload->>'tool_call_id'=%s",
            (event_type,str(tool_call_id))).fetchone()[0]


def test_immediate_clock_tool_uses_typed_timezone_without_operation(dsn):
    user,instance=seed(dsn)
    turn=incoming(dsn,user,instance,"What time is it?")
    now=datetime(2026,11,11,9,0,tzinfo=timezone.utc)
    reply=invoke(
        dsn,respond_with_time,actor(user),turn.turn_id,
        timezone_name="Europe/Berlin",now=now)
    assert reply.text=="Current time in Europe/Berlin: 2026-11-11 10:00:00 CET"
    assert reply.model_id=="clock.now.v1"
    assert finish_initial(dsn,user,reply.response_id).delivery_status=="DELIVERED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",
            (user,)).fetchone()[0]==0
    with pytest.raises(ValueError):
        invoke(dsn,respond_with_time,actor(user),turn.turn_id,
               timezone_name="Not/A_Real_Timezone")


def test_staged_admission_recovers_after_missing_pool_and_is_idempotent(dsn):
    user,instance=seed(dsn)
    turn=incoming(dsn,user,instance)
    command=SlowTextToolCommand(turn_id=turn.turn_id,max_words=2)
    with pytest.raises(ResourceUnavailable):
        invoke(dsn,schedule_text_analysis,actor(user),command)
    with psycopg.connect(dsn,autocommit=True) as conn:
        stage=conn.execute(
            "SELECT tool_call_id,operation_id,admission_state "
            "FROM conversation_tool_calls WHERE turn_id=%s",
            (turn.turn_id,)).fetchone()
        assert stage[1:]==(None,"PREPARING")
    configure_tool_pool(dsn)
    first=invoke(dsn,schedule_text_analysis,actor(user),command)
    replay=invoke(dsn,schedule_text_analysis,actor(user),command)
    assert first.tool_call_id==stage[0]==first.operation_id
    assert replay.operation_id==first.operation_id
    assert first.admission_state=="QUEUED"
    with pytest.raises(IdempotencyConflict):
        invoke(dsn,schedule_text_analysis,actor(user),
               SlowTextToolCommand(turn_id=turn.turn_id,max_words=3))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id=%s",
            (first.operation_id,)).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='conversation.tool.requested.v1' "
            "AND payload->>'tool_call_id'=%s",
            (str(first.tool_call_id),)).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='operation.queued.v1' "
            "AND payload->>'operation_id'=%s",
            (str(first.operation_id),)).fetchone()[0]==1


def test_slow_tool_deferred_result_recovered_after_restart_and_ack_once(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    cmd=SlowTextToolCommand(turn_id=turn.turn_id,max_words=2)
    # No worker is running: the bounded foreground wait returns a short ack.
    reply=invoke(dsn,respond_to_text_analysis,actor(user),cmd,await_seconds=0.01)
    assert reply.model_id=="tool.initial.deferred.v1"
    assert "follow up" in reply.text
    finish_initial(dsn,user,reply.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        tool_id=conn.execute(
            "SELECT tool_call_id FROM conversation_tool_calls WHERE turn_id=%s",
            (turn.turn_id,)).fetchone()[0]
    # Simulate separate worker/process with a fresh connection.
    lease=invoke(dsn,run_one_text_tool,worker_id="tool-worker")
    assert lease is not None and lease.operation_id==tool_id
    assert invoke(dsn,run_one_text_tool,worker_id="tool-worker-2") is None
    state=invoke(dsn,reconcile_tool_followup,actor(user),tool_id)
    assert state.followup_state=="READY" and state.initial_reply_mode=="DEFERRED"
    # A new process reconstructs the result solely from the database.
    follow=invoke(dsn,prepare_tool_followup,actor(user),tool_id)
    assert follow.text=="Text analysis: 4 words, 3 unique. Top: beta (2), alpha (1)."
    done=invoke(dsn,acknowledge_tool_followup,actor(user),tool_id)
    replay=invoke(dsn,acknowledge_tool_followup,actor(user),tool_id)
    assert done.text==follow.text and not done.replayed
    assert replay.replayed
    assert event_count(dsn,tool_id,"conversation.tool.followup_delivered.v1")==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM outbox o JOIN events e ON e.event_id=o.event_id "
            "WHERE e.event_type='conversation.tool.followup_delivered.v1' "
            "AND e.payload->>'tool_call_id'=%s",
            (str(tool_id),)).fetchone()[0]==1


def test_awaited_completion_uses_direct_reply_and_no_duplicate_followup(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance,"foo foo and bar")
    tool=invoke(
        dsn,schedule_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=turn.turn_id))
    invoke(dsn,run_one_text_tool,worker_id="fast-background-worker")
    reply=invoke(
        dsn,respond_to_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=turn.turn_id),await_seconds=0.5)
    assert reply.model_id=="tool.initial.direct.v1"
    assert reply.text.startswith("Text analysis: 4 words")
    finish_initial(dsn,user,reply.response_id)
    state=invoke(dsn,reconcile_tool_followup,actor(user),tool.tool_call_id)
    assert state.initial_reply_mode=="DIRECT"
    assert state.followup_state=="SUPPRESSED"
    with pytest.raises(ToolResultNotReady):
        invoke(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)


def test_completed_result_waits_for_initial_ack_before_followup(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    reply=invoke(
        dsn,respond_to_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=turn.turn_id),await_seconds=0)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    invoke(dsn,run_one_text_tool,worker_id="other-worker")
    pending=invoke(dsn,reconcile_tool_followup,actor(user),tool.tool_call_id)
    assert pending.followup_state=="DEFERRED"
    with pytest.raises(ToolResultNotReady):
        invoke(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)
    finish_initial(dsn,user,reply.response_id)
    assert invoke(dsn,reconcile_tool_followup,actor(user),
                  tool.tool_call_id).followup_state=="READY"


def test_new_message_defers_late_result_instead_of_interrupting(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    conv=uuid4()
    turn=incoming(dsn,user,instance,conversation=conv)
    reply=invoke(
        dsn,respond_to_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=turn.turn_id),await_seconds=0)
    finish_initial(dsn,user,reply.response_id)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    incoming(dsn,user,instance,"Unrelated new topic",conversation=conv)
    invoke(dsn,run_one_text_tool,worker_id="deferred-worker")
    assert invoke(dsn,reconcile_tool_followup,actor(user),
                  tool.tool_call_id).followup_state=="DEFERRED"
    with pytest.raises(ToolResultNotReady):
        invoke(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)


def test_persona_switch_suppresses_late_tool_followup(dsn):
    user,instance,other=seed(dsn,two=True)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    reply=invoke(
        dsn,respond_to_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=turn.turn_id),await_seconds=0)
    finish_initial(dsn,user,reply.response_id)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    invoke(dsn,run_one_text_tool,worker_id="switch-worker")
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4())))
    state=invoke(dsn,reconcile_tool_followup,actor(user),tool.tool_call_id)
    assert state.followup_state=="SUPPRESSED"
    with pytest.raises(ToolResultNotReady):
        invoke(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)
    assert invoke(dsn,read_tool_call,actor(user),
                  tool.tool_call_id).followup_state=="SUPPRESSED"


def test_worker_filters_other_queued_operations(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    other_pool=f"other-{uuid4()}"
    invoke(dsn,configure_resource_pool,other_pool,capacity_units=1)
    unrelated=EnqueueOperation(
        operation_id=uuid4(),instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),operation_type="other.domain.task",
        execution_policy=Policy.FOREGROUND_ASYNC,priority_class=Priority.P1,
        effect_class=EffectClass.NONE,resources={other_pool:1},
    )
    invoke(dsn,enqueue_operation,actor(user),unrelated)
    turn=incoming(dsn,user,instance)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    lease=invoke(dsn,run_one_text_tool,worker_id="typed-tool-worker")
    assert lease.operation_id==tool.operation_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status,attempt_count FROM operations WHERE operation_id=%s",
            (unrelated.operation_id,)).fetchone()==("QUEUED",0)


def test_worker_failure_never_publishes_unconfirmed_result(dsn):
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    reply=invoke(dsn,respond_to_text_analysis,actor(user),
                 SlowTextToolCommand(turn_id=turn.turn_id),await_seconds=0)
    finish_initial(dsn,user,reply.response_id)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    def failed(text,max_words):
        raise RuntimeError("synthetic worker failure")
    invoke(dsn,run_one_text_tool,worker_id="broken-worker",executor=failed)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (tool.operation_id,)).fetchone()[0]=="RETRY_WAIT"
        assert conn.execute(
            "SELECT count(*) FROM conversation_tool_results WHERE operation_id=%s",
            (tool.operation_id,)).fetchone()[0]==0
    assert invoke(dsn,reconcile_tool_followup,actor(user),
                  tool.tool_call_id).followup_state=="PENDING"


def test_scoped_tool_status_and_followup_deny_other_user(dsn):
    user,instance=seed(dsn)
    stranger,_=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    tool=invoke(dsn,schedule_text_analysis,actor(user),
                SlowTextToolCommand(turn_id=turn.turn_id))
    with pytest.raises(PermissionDenied):
        invoke(dsn,read_tool_call,actor(stranger),tool.tool_call_id)
    with pytest.raises(PermissionDenied):
        invoke(dsn,reconcile_tool_followup,actor(stranger),tool.tool_call_id)



def test_staging_reconciles_after_crash_between_enqueue_and_link(dsn):
    """P0 enqueue receipt survives a crash before P1 correlation is finalized."""
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    turn=incoming(dsn,user,instance)
    request=SlowTextToolCommand(turn_id=turn.turn_id)
    scheduled=invoke(dsn,schedule_text_analysis,actor(user),request)
    with psycopg.connect(dsn,autocommit=True) as conn:
        # Simulate the exact durable state present immediately after a
        # committed P0 enqueue but before the P1 post-enqueue update.
        conn.execute(
            "UPDATE conversation_tool_calls SET operation_id=NULL,"
            "admission_state='PREPARING' WHERE tool_call_id=%s",
            (scheduled.tool_call_id,),
        )
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id=%s",
            (scheduled.operation_id,),
        ).fetchone()[0]==1
    restored=invoke(dsn,schedule_text_analysis,actor(user),request)
    assert restored.tool_call_id==scheduled.tool_call_id
    assert restored.operation_id==scheduled.operation_id
    assert restored.admission_state=="QUEUED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id=%s",
            (scheduled.operation_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='operation.queued.v1' "
            "AND payload->>'operation_id'=%s",
            (str(scheduled.operation_id),),
        ).fetchone()[0]==1


def test_new_user_turn_does_not_wait_for_running_readonly_tool(dsn):
    """The slow worker's provider call cannot retain a conversation DB lock."""
    user,instance=seed(dsn)
    configure_tool_pool(dsn)
    conversation=uuid4()
    first=incoming(dsn,user,instance,conversation=conversation)
    response=invoke(
        dsn,respond_to_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=first.turn_id),await_seconds=0,
    )
    finish_initial(dsn,user,response.response_id)
    tool=invoke(
        dsn,schedule_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=first.turn_id),
    )
    running=Event()
    release=Event()

    def blocked_tool(text,max_words):
        running.set()
        assert release.wait(timeout=10), "test did not release tool"
        return "Delayed read-only result"

    with ThreadPoolExecutor(max_workers=1) as workers:
        job=workers.submit(
            invoke,dsn,run_one_text_tool,
            worker_id="slow-while-foreground-active",
            executor=blocked_tool,
        )
        try:
            assert running.wait(timeout=10)
            # A separate primary-user connection must commit while the tool
            # is still running, rather than waiting for a held DB lock.
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute("SET lock_timeout='500ms'")
                new=ingest_text(
                    conn,actor(user),TextInputCommand(
                        conversation_id=conversation,
                        instance_id=instance,
                        expected_generation=1,
                        idempotency_key=f"concurrent-{uuid4()}",
                        text="New topic while tool executes",
                    ),
                )
                assert new.sequence==2
        finally:
            release.set()
        lease=job.result(timeout=10)
    assert lease.operation_id==tool.operation_id
    assert invoke(
        dsn,reconcile_tool_followup,actor(user),tool.tool_call_id,
    ).followup_state=="DEFERRED"



def test_tool_correlation_cannot_reference_another_users_operation(dsn):
    user,instance=seed(dsn)
    other,other_instance=seed(dsn)
    configure_tool_pool(dsn)
    own_turn=incoming(dsn,user,instance)
    linked=invoke(
        dsn,schedule_text_analysis,actor(user),
        SlowTextToolCommand(turn_id=own_turn.turn_id),
    )
    # Use an otherwise-unlinked operation from the other user, so the
    # composite tenant FK (rather than the operation_id UNIQUE constraint)
    # is what rejects the forged correlation.
    foreign=EnqueueOperation(
        operation_id=uuid4(),instance_id=other_instance,
        expected_generation=1,idempotency_key=str(uuid4()),
        operation_type="unrelated.readonly",
        execution_policy=Policy.FOREGROUND_ASYNC,
        priority_class=Priority.P1,effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},
    )
    invoke(dsn,enqueue_operation,actor(other),foreign)
    from psycopg.errors import ForeignKeyViolation
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(ForeignKeyViolation):
            conn.execute(
                "UPDATE conversation_tool_calls SET operation_id=%s "
                "WHERE tool_call_id=%s",
                (foreign.operation_id,linked.tool_call_id),
            )
        assert conn.execute(
            "SELECT operation_id FROM conversation_tool_calls "
            "WHERE tool_call_id=%s",
            (linked.tool_call_id,),
        ).fetchone()==(linked.operation_id,)
