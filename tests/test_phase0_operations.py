"""P0-04 PostgreSQL resource, authority, fencing and recovery acceptance tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
import time
from uuid import uuid4

import pytest

psycopg=pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus

from persona_agent.contracts import TrustedActor
from persona_agent.lifecycle import (
    BackgroundReason, EnterBackgroundCommand, PrimaryActivityCommand,
    SwitchPersonaCommand, TrustedPlatformActor, enter_background,
    record_primary_activity, switch_persona,
)
from persona_agent.operation_contracts import (
    EffectClass, EnqueueOperation, InvalidOperation, Origin, Policy, Priority,
    ResourceUnavailable, StaleOperation, Status,
)
from persona_agent.operation_scheduler import (
    claim_operation, configure_resource_pool, enqueue_operation,
)
from persona_agent.operation_control import (
    CancelOperationCommand, acknowledge_cancel, cancel_operation,
    checkpoint_operation, complete_operation, fail_operation,
    heartbeat_operation, mark_external_dispatched, reap_stale_operations,
    reconcile_unknown, resume_suspended,
)
from persona_agent.runtime import IdempotencyConflict, PermissionDenied, StaleSelection


@pytest.fixture(autouse=True)
def isolate_operation_queue(dsn):
    """Keep independently seeded test operations from prior tests off the queue.

    Each test intentionally uses the same integration PostgreSQL database;
    production consumers operate globally, so orphaned QUEUED rows would
    otherwise be legitimately claimed by later worker tests.
    """
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM operation_concurrency_locks")
            conn.execute("DELETE FROM operation_resource_reservations")
            conn.execute("UPDATE execution_resource_pools SET active_units=0")
            conn.execute(
                """
                UPDATE operations SET status='CANCELLED',
                    worker_id=NULL,lease_until=NULL,claimed_at=NULL
                WHERE status IN (
                    'QUEUED','RUNNING','RETRY_WAIT','CANCEL_REQUESTED',
                    'SUSPEND_REQUESTED','SUSPENDED','BLOCKED'
                )
                """,
            )


def seed(dsn, two=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"persona-{persona}"),
            )
            conn.execute(
                """
                INSERT INTO persona_instances(id,user_id,persona_id,label)
                VALUES (%s,%s,%s,'One')
                """,(instance,user,persona),
            )
            if two:
                p2,i2=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"persona-{p2}"),
                )
                conn.execute(
                    """
                    INSERT INTO persona_instances(id,user_id,persona_id,label)
                    VALUES (%s,%s,%s,'Two')
                    """,(i2,user,p2),
                )
            conn.execute(
                """
                INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                VALUES (%s,%s,1,'FOREGROUND')
                """,(user,instance),
            )
    return (user,instance,i2) if two else (user,instance)


def actor(user):
    return TrustedActor(user_id=user,source_id="authenticated-user-test")


def platform(user):
    return TrustedPlatformActor(user_id=user,source_id="trusted-scheduler-test")


def invoke(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def pool(dsn, capacity=2, reserve=0):
    key="pool-"+str(uuid4())
    invoke(
        dsn,configure_resource_pool,key,capacity_units=capacity,
        p0_reserved_units=reserve,
    )
    return key


def cmd(instance, resource, *,
        generation=1, priority=Priority.P1, policy=Policy.FOREGROUND_ASYNC,
        effect=EffectClass.NONE, origin=Origin.USER, operation_id=None,
        key=None, dependencies=(), capabilities=(), concurrency_group=None,
        max_attempts=3, deadline=None, units=1, input_versions=None, timeout_seconds=300):
    return EnqueueOperation(
        operation_id=operation_id or uuid4(),instance_id=instance,
        expected_generation=generation,idempotency_key=key or str(uuid4()),
        operation_type="test.operation",origin=origin,
        execution_policy=policy,priority_class=priority,
        effect_class=effect,resources={resource:units},
        dependencies=dependencies,capabilities=capabilities,
        concurrency_group=concurrency_group,max_attempts=max_attempts,
        deadline=deadline,input_versions=input_versions or {},
        timeout_seconds=timeout_seconds,
    )


def enqueue(dsn,user,command):
    return invoke(dsn,enqueue_operation,actor(user),command)


def claim(dsn, worker="w", *, capabilities=()):
    return invoke(
        dsn,claim_operation,worker_id=worker,capabilities=capabilities,
        lease_seconds=60,
    )


def operation(dsn, op_id):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(
            """
            SELECT status,attempt_count,claim_generation,selection_generation,
                   background_epoch,effect_status,version,result_ref
            FROM operations WHERE operation_id=%s
            """,(op_id,),
        ).fetchone()


def resources(dsn, key):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(
            "SELECT active_units FROM execution_resource_pools WHERE resource_key=%s",
            (key,),
        ).fetchone()[0]


def test_enqueue_is_atomic_idempotent_and_event_sourced(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    command=cmd(instance,key)
    first=enqueue(dsn,user,command)
    duplicate=enqueue(dsn,user,command)
    assert first.status == Status.QUEUED and not first.replayed
    assert duplicate.replayed and duplicate.event_id==first.event_id
    assert duplicate.operation_id==first.operation_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id=%s",
            (first.operation_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",
            (first.event_id,),
        ).fetchone()[0] == 1
    bad=command.model_copy(update={"operation_type":"different"})
    with pytest.raises(IdempotencyConflict):
        enqueue(dsn,user,bad)


def test_user_isolation_and_obsolete_generation_reject_enqueue(dsn):
    user,instance=seed(dsn)
    another,other=seed(dsn)
    key=pool(dsn)
    with pytest.raises(PermissionDenied):
        enqueue(dsn,another,cmd(instance,key))
    with pytest.raises(StaleSelection):
        enqueue(dsn,user,cmd(instance,key,generation=2))
    with pytest.raises(PermissionDenied):
        enqueue(dsn,user,cmd(other,key))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",(user,),
        ).fetchone()[0]==0


def test_competing_workers_claim_once_and_release_reservation(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=1)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(claim,dsn,"worker-a")
        b=executor.submit(claim,dsn,"worker-b")
        wins=[a.result(),b.result()]
    assert sum(v is not None and v.operation_id==c.operation_id for v in wins)==1
    lease=next(v for v in wins if v is not None)
    assert resources(dsn,key)==1
    invoke(dsn,complete_operation,lease,result_ref="object://result/1")
    assert resources(dsn,key)==0
    assert operation(dsn,c.operation_id)[0]=="COMPLETED"
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="duplicated")


def test_capability_and_dependency_gates(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    parent=cmd(instance,key,capabilities=("model",))
    child=cmd(instance,key,dependencies=(parent.operation_id,))
    enqueue(dsn,user,parent)
    enqueue(dsn,user,child)
    assert claim(dsn,"no-capability")==None
    lease=claim(dsn,"worker-with-model",capabilities=("model",))
    assert lease.operation_id==parent.operation_id
    assert claim(dsn,"worker-for-child") is None
    invoke(dsn,complete_operation,lease,result_ref="parent-result")
    next_lease=claim(dsn,"worker-for-child")
    assert next_lease.operation_id==child.operation_id
    invoke(dsn,complete_operation,next_lease,result_ref="child-result")


def test_other_personas_cannot_supply_dependencies(dsn):
    user,instance=seed(dsn)
    other_user,other_instance=seed(dsn)
    key=pool(dsn)
    first=cmd(other_instance,key)
    enqueue(dsn,other_user,first)
    with pytest.raises(PermissionDenied):
        enqueue(dsn,user,cmd(instance,key,dependencies=(first.operation_id,)))


def test_p0_capacity_reservation_and_priority(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=2,reserve=1)
    p1a=cmd(instance,key)
    p1b=cmd(instance,key)
    enqueue(dsn,user,p1a)
    first=claim(dsn,"p1-running")
    assert first.operation_id==p1a.operation_id
    enqueue(dsn,user,p1b)
    assert claim(dsn,"p1-blocked") is None
    p0=cmd(instance,key,priority=Priority.P0)
    enqueue(dsn,user,p0)
    high=claim(dsn,"interactive")
    assert high.operation_id==p0.operation_id
    assert resources(dsn,key)==2
    invoke(dsn,complete_operation,high,result_ref="high")
    assert claim(dsn,"p1-still-blocked") is None
    invoke(dsn,complete_operation,first,result_ref="first")
    second=claim(dsn,"p1-admitted")
    assert second.operation_id==p1b.operation_id
    invoke(dsn,complete_operation,second,result_ref="second")
    assert resources(dsn,key)==0


def test_group_exclusion_and_independent_operations(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=3)
    x=cmd(instance,key,concurrency_group="repository-writer")
    y=cmd(instance,key,concurrency_group="repository-writer")
    z=cmd(instance,key)
    for c in (x,y,z):
        enqueue(dsn,user,c)
    first=claim(dsn,"one")
    assert first.operation_id==x.operation_id
    next_op=claim(dsn,"other")
    assert next_op.operation_id==z.operation_id
    invoke(dsn,complete_operation,first,result_ref="one-result")
    last=claim(dsn,"another")
    assert last.operation_id==y.operation_id
    invoke(dsn,complete_operation,next_op,result_ref="two-result")
    invoke(dsn,complete_operation,last,result_ref="three-result")
    assert resources(dsn,key)==0


def test_retry_exhaustion_and_fenced_old_attempt(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,max_attempts=2)
    enqueue(dsn,user,c)
    lease=claim(dsn,"old")
    assert invoke(dsn,fail_operation,lease,reason="transient",delay_seconds=0)==Status.RETRY_WAIT
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="wrong")
    retry=claim(dsn,"new")
    assert retry.attempt_count==2 and retry.claim_generation==2
    assert invoke(dsn,fail_operation,retry,reason="failed again",delay_seconds=0)==Status.FAILED
    assert claim(dsn) is None
    assert resources(dsn,key)==0


def test_heartbeat_checkpoint_and_expired_claim_recovery(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    lease=claim(dsn)
    refreshed=invoke(dsn,heartbeat_operation,lease,extend_seconds=40)
    assert refreshed.claim_generation==lease.claim_generation
    invoke(dsn,checkpoint_operation,refreshed,checkpoint_ref="checkpoint://v1")
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",(c.operation_id,),
        )
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,refreshed,result_ref="late")
    assert invoke(dsn,reap_stale_operations)>=1
    assert resources(dsn,key)==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp() WHERE operation_id=%s",
            (c.operation_id,),
        )
    successor=claim(dsn,"after-crash")
    assert successor.claim_generation==lease.claim_generation+1
    assert operation(dsn,c.operation_id)[0]=="RUNNING"
    invoke(dsn,complete_operation,successor,result_ref="resumed")


def test_switch_invalidates_worker_and_recovery_suspends(dsn):
    user,instance,alternative=seed(dsn,two=True)
    key=pool(dsn)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    lease=claim(dsn)
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=alternative,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="stale-result")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="SUSPENDED"
    assert resources(dsn,key)==0
    with pytest.raises(StaleOperation):
        invoke(dsn,resume_suspended,actor(user),operation_id=c.operation_id)
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=alternative,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    invoke(dsn,resume_suspended,actor(user),operation_id=c.operation_id)
    new=claim(dsn,"new-generation")
    assert new.selection_generation==3
    invoke(dsn,complete_operation,new,result_ref="fresh")


def test_background_epoch_gates_ephemeral_result(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,c)
    assert claim(dsn) is None
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    lease=claim(dsn)
    assert lease.background_epoch==1
    invoke(dsn,record_primary_activity,actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleOperation):
        invoke(dsn,checkpoint_operation,lease,checkpoint_ref="bad")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="CANCELLED"
    assert resources(dsn,key)==0


def test_background_autonomy_gate_requires_preparation(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(
        instance,key,priority=Priority.P2,
        policy=Policy.BACKGROUND_RESUMABLE,origin=Origin.USER,
    )
    enqueue(dsn,user,c)
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    assert claim(dsn) is None
    # This privileged SQL fixture represents a future completed Phase 4
    # preparation barrier. No runtime function can manufacture readiness.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO background_ready_epochs(user_id,instance_id,background_epoch)
            VALUES (%s,%s,1)
            """,(user,instance),
        )
    lease=claim(dsn)
    assert lease.operation_id==c.operation_id and lease.background_epoch==1
    invoke(dsn,record_primary_activity,actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="stale-bg")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="SUSPENDED"


def test_user_cancel_before_claim_and_running_worker_ack(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    queued=cmd(instance,key)
    enqueue(dsn,user,queued)
    stopped=invoke(dsn,cancel_operation,actor(user),CancelOperationCommand(
        operation_id=queued.operation_id,expected_version=0,idempotency_key="q-cancel",
    ))
    assert stopped==Status.CANCELLED
    assert resources(dsn,key)==0
    assert claim(dsn) is None
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    lease=claim(dsn)
    status=invoke(dsn,cancel_operation,actor(user),CancelOperationCommand(
        operation_id=c.operation_id,expected_version=1,
        idempotency_key="r-cancel",
    ))
    assert status==Status.CANCEL_REQUESTED
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="ignored")
    invoke(dsn,acknowledge_cancel,lease)
    assert operation(dsn,c.operation_id)[0]=="CANCELLED"
    assert resources(dsn,key)==0


def test_cancel_is_idempotent_and_versioned(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    command=CancelOperationCommand(
        operation_id=c.operation_id,expected_version=0,idempotency_key="repeat",
    )
    assert invoke(dsn,cancel_operation,actor(user),command)==Status.CANCELLED
    assert invoke(dsn,cancel_operation,actor(user),command)==Status.CANCELLED
    with pytest.raises(IdempotencyConflict):
        invoke(dsn,cancel_operation,actor(user),command.model_copy(
            update={"expected_version":1},
        ))


def test_unknown_external_effect_stays_quarantined_until_evidenced(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=1)
    external=cmd(instance,key,effect=EffectClass.EXTERNAL)
    follower=cmd(instance,key)
    enqueue(dsn,user,external)
    lease=claim(dsn)
    assert lease.operation_id==external.operation_id
    invoke(dsn,mark_external_dispatched,lease)
    with pytest.raises(InvalidOperation):
        invoke(dsn,mark_external_dispatched,lease)
    enqueue(dsn,user,follower)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",(external.operation_id,),
        )
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,external.operation_id)[0]=="OUTCOME_UNKNOWN"
    assert resources(dsn,key)==1  # do not release exclusive external effect lock
    assert claim(dsn,"should-be-blocked") is None
    assert invoke(
        dsn,reconcile_unknown,platform(user),operation_id=external.operation_id,
        confirmed=True,evidence_ref="provider://verified/123",
    )==Status.COMPLETED
    assert resources(dsn,key)==0
    next_lease=claim(dsn,"after-reconcile")
    assert next_lease.operation_id==follower.operation_id
    invoke(dsn,complete_operation,next_lease,result_ref="follower")


def test_no_unauthorized_autonomous_goal_creation(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(
        instance,key,priority=Priority.P2,
        policy=Policy.BACKGROUND_RESUMABLE,origin=Origin.GOAL,
    )
    with pytest.raises(PermissionDenied):
        enqueue(dsn,user,c)
    assert operation(dsn,c.operation_id) is None


def test_infeasible_resources_and_missing_pools_fail_at_admission(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=2,reserve=1)
    with pytest.raises(ResourceUnavailable):
        enqueue(dsn,user,cmd(instance,key,units=2))
    with pytest.raises(ResourceUnavailable):
        enqueue(dsn,user,cmd(instance,"unconfigured-"+str(uuid4())))


def test_nested_autocommit_rejected_and_no_event_emitted(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            assert conn.info.transaction_status==TransactionStatus.INTRANS
            with pytest.raises(ValueError,match="idle autocommit"):
                enqueue_operation(conn,actor(user),c)
            with pytest.raises(ValueError,match="idle autocommit"):
                claim_operation(conn,worker_id="w")
            with pytest.raises(ValueError,match="idle autocommit"):
                reap_stale_operations(conn)
    assert operation(dsn,c.operation_id) is None



def test_declared_input_version_is_checked_before_effects(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,input_versions={"persona_instance":0})
    enqueue(dsn,user,c)
    lease=claim(dsn)
    from persona_agent.contracts import RenameInstanceCommand
    from persona_agent.runtime import rename_instance
    invoke(dsn,rename_instance,actor(user),RenameInstanceCommand(
        instance_id=instance,expected_generation=1,expected_version=0,
        new_label="changed",idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleOperation,match="input state version"):
        invoke(dsn,complete_operation,lease,result_ref="invalid-stale-result")
    with pytest.raises(StaleOperation,match="input state version"):
        invoke(dsn,mark_external_dispatched,lease)
    assert operation(dsn,c.operation_id)[0]=="RUNNING"


def test_wrong_initial_input_version_and_unknown_owner_rejected(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    from persona_agent.runtime import VersionConflict
    with pytest.raises(VersionConflict):
        enqueue(dsn,user,cmd(instance,key,input_versions={"persona_instance":12}))
    with pytest.raises(ValueError,match="unknown input version owner"):
        cmd(instance,key,input_versions={"goal":1})


def test_timeout_is_capped_at_claim_and_reaped_after_absolute_deadline(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,timeout_seconds=1)
    enqueue(dsn,user,c)
    lease=claim(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        recorded=conn.execute(
            "SELECT claimed_at,lease_until FROM operations WHERE operation_id=%s",
            (c.operation_id,),
        ).fetchone()
        assert recorded[1] <= recorded[0]+timedelta(seconds=1,milliseconds=25)
        conn.execute(
            """
            UPDATE operations SET
              claimed_at=clock_timestamp()-interval '2 seconds'
            WHERE operation_id=%s
            """,(c.operation_id,),
        )
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="RETRY_WAIT"
    assert resources(dsn,key)==0


def test_background_ephemeral_is_cancelled_on_persona_switch(dsn):
    user,instance,alternative=seed(dsn,two=True)
    key=pool(dsn)
    c=cmd(instance,key,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,c)
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    lease=claim(dsn)
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=alternative,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleOperation):
        invoke(dsn,checkpoint_operation,lease,checkpoint_ref="should-not-commit")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="CANCELLED"


def test_cancellation_receipt_references_event_and_outbox(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    invoke(dsn,cancel_operation,actor(user),CancelOperationCommand(
        operation_id=c.operation_id,expected_version=0,
        idempotency_key="track-cancel",
    ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """
            SELECT count(*) FROM command_receipts r
            JOIN events e ON r.event_id=e.event_id
            JOIN outbox o ON o.event_id=e.event_id
            WHERE r.command_type='operation.cancel.v1'
              AND r.user_id=%s AND r.idempotency_key='track-cancel'
            """,(user,),
        ).fetchone()[0]==1



def test_deadline_is_checked_during_execution_and_crash_recovery(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,deadline=datetime.now(timezone.utc)+timedelta(seconds=20))
    enqueue(dsn,user,c)
    lease=claim(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """
            UPDATE operations SET deadline=created_at+interval '1 millisecond'
            WHERE operation_id=%s
            """,(c.operation_id,),
        )
        conn.execute("SELECT pg_sleep(0.015)")
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="after-deadline")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="FAILED"
    assert resources(dsn,key)==0


def test_authenticated_user_cannot_forge_cognitive_origin(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    with pytest.raises(PermissionDenied,match="agent-originated"):
        enqueue(dsn,user,cmd(instance,key,origin=Origin.COGNITIVE))



def test_queued_operation_is_suspended_after_persona_switch(dsn):
    user,instance,alternative=seed(dsn,two=True)
    key=pool(dsn)
    c=cmd(instance,key)
    enqueue(dsn,user,c)
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=alternative,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="SUSPENDED"
    assert resources(dsn,key)==0
    invoke(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=alternative,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    invoke(dsn,resume_suspended,actor(user),operation_id=c.operation_id)
    lease=claim(dsn)
    assert lease.operation_id==c.operation_id and lease.selection_generation==3
    invoke(dsn,complete_operation,lease,result_ref="recovered-after-switch")


def test_background_retry_wait_is_cancelled_when_epoch_invalidated(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,c)
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    lease=claim(dsn)
    assert invoke(dsn,fail_operation,lease,reason="retry",delay_seconds=0)==Status.RETRY_WAIT
    assert operation(dsn,c.operation_id)[0]=="RETRY_WAIT"
    invoke(dsn,record_primary_activity,actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="CANCELLED"
    assert claim(dsn) is None


def test_expired_queued_operation_cannot_remain_live(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,deadline=datetime.now(timezone.utc)+timedelta(seconds=10))
    enqueue(dsn,user,c)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET deadline=created_at+interval '1 millisecond' "
            "WHERE operation_id=%s",(c.operation_id,),
        )
        conn.execute("SELECT pg_sleep(0.015)")
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="FAILED"
    assert claim(dsn) is None



def test_stale_input_version_is_reconciled_to_blocked_before_claim(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,input_versions={"persona_instance":0})
    enqueue(dsn,user,c)
    from persona_agent.contracts import RenameInstanceCommand
    from persona_agent.runtime import rename_instance
    invoke(dsn,rename_instance,actor(user),RenameInstanceCommand(
        instance_id=instance,expected_generation=1,expected_version=0,
        new_label="updated",idempotency_key=str(uuid4()),
    ))
    assert claim(dsn) is None
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="BLOCKED"
    assert resources(dsn,key)==0


def test_running_stale_input_version_releases_reservation_on_reconciliation(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,input_versions={"persona_instance":0})
    enqueue(dsn,user,c)
    lease=claim(dsn)
    assert resources(dsn,key)==1
    from persona_agent.contracts import RenameInstanceCommand
    from persona_agent.runtime import rename_instance
    invoke(dsn,rename_instance,actor(user),RenameInstanceCommand(
        instance_id=instance,expected_generation=1,expected_version=0,
        new_label="updated",idempotency_key=str(uuid4()),
    ))
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,c.operation_id)[0]=="BLOCKED"
    assert resources(dsn,key)==0
    with pytest.raises(StaleOperation):
        invoke(dsn,complete_operation,lease,result_ref="stale-worker-result")



def test_elapsed_deadline_replay_remains_idempotent(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(
        instance,key,
        deadline=datetime.now(timezone.utc)+timedelta(seconds=1),
    )
    initial=enqueue(dsn,user,c)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("SELECT pg_sleep(1.15)")
    repeated=enqueue(dsn,user,c)
    assert repeated.replayed and repeated.event_id==initial.event_id
    assert repeated.operation_id==initial.operation_id


def test_new_admission_with_past_deadline_is_rejected(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    c=cmd(instance,key,deadline=datetime.now(timezone.utc)-timedelta(seconds=1))
    with pytest.raises(InvalidOperation,match="deadline"):
        enqueue(dsn,user,c)
    assert operation(dsn,c.operation_id) is None



def test_claim_scans_past_default_40_older_capability_mismatches(dsn):
    specialist_user, specialist_instance = seed(dsn)
    general_user, general_instance = seed(dsn)
    key = pool(dsn,capacity=1)
    # These higher-priority, globally queued operations cannot run on this
    # worker. They used to occupy the entire default LIMIT 40 forever.
    for _ in range(42):
        enqueue(
            dsn, specialist_user,
            cmd(specialist_instance,key,priority=Priority.P0,
                capabilities=("gpu-specialist",)),
        )
    runnable = cmd(general_instance,key,priority=Priority.P1)
    enqueue(dsn,general_user,runnable)
    lease = claim(dsn,"general-worker")
    assert lease is not None and lease.operation_id == runnable.operation_id
    assert lease.user_id == general_user
    invoke(dsn,complete_operation,lease,result_ref="general-finished")
    assert resources(dsn,key)==0


def test_claim_paginates_past_resource_blocked_candidates(dsn):
    user,instance=seed(dsn)
    saturated = pool(dsn,capacity=1)
    available = pool(dsn,capacity=1)
    holder=cmd(instance,saturated)
    enqueue(dsn,user,holder)
    running=claim(dsn,"capacity-holder")
    assert running.operation_id==holder.operation_id
    for _ in range(8):
        enqueue(dsn,user,cmd(instance,saturated))
    last=cmd(instance,available)
    enqueue(dsn,user,last)
    lease=invoke(
        dsn,claim_operation,worker_id="other-resource-worker",
        capabilities=(),lease_seconds=60,scan_limit=2,
    )
    assert lease is not None and lease.operation_id==last.operation_id
    invoke(dsn,complete_operation,lease,result_ref="unblocked")
    invoke(dsn,complete_operation,running,result_ref="holder")
    assert resources(dsn,saturated)==0 and resources(dsn,available)==0


def test_claim_paginates_past_background_only_candidates(dsn):
    user,instance=seed(dsn)
    key=pool(dsn,capacity=2)
    for _ in range(7):
        enqueue(dsn,user,cmd(instance,key,policy=Policy.BACKGROUND_EPHEMERAL))
    fg=cmd(instance,key,policy=Policy.FOREGROUND_ASYNC)
    enqueue(dsn,user,fg)
    lease=invoke(
        dsn,claim_operation,worker_id="interactive",
        capabilities=(),lease_seconds=60,scan_limit=2,
    )
    assert lease is not None and lease.operation_id==fg.operation_id
    invoke(dsn,complete_operation,lease,result_ref="foreground")


def test_committed_enqueue_replays_after_input_version_changes(dsn):
    user,instance=seed(dsn)
    key=pool(dsn)
    command=cmd(instance,key,input_versions={"persona_instance":0})
    original=enqueue(dsn,user,command)
    from persona_agent.contracts import RenameInstanceCommand
    from persona_agent.runtime import rename_instance, VersionConflict
    invoke(dsn,rename_instance,actor(user),RenameInstanceCommand(
        instance_id=instance,expected_generation=1,expected_version=0,
        new_label="changed",idempotency_key=str(uuid4()),
    ))
    replay=enqueue(dsn,user,command)
    assert replay.replayed
    assert replay.event_id==original.event_id
    assert replay.operation_id==original.operation_id
    assert operation(dsn,command.operation_id)[0]=="QUEUED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='operation.queued.v1' "
            "AND instance_id=%s",(instance,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",
            (original.event_id,),
        ).fetchone()[0]==1

    # An identical key but different fingerprint must still conflict;
    # a genuinely new key with the stale version must fail admission.
    with pytest.raises(IdempotencyConflict):
        enqueue(dsn,user,command.model_copy(update={"operation_type":"different"}))
    with pytest.raises(VersionConflict):
        enqueue(dsn,user,cmd(instance,key,input_versions={"persona_instance":0}))



def test_ephemeral_admitted_during_background_cannot_run_next_epoch(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    pending=cmd(instance,resource,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,pending)
    with psycopg.connect(dsn,autocommit=True) as conn:
        stored=conn.execute(
            "SELECT status,background_epoch FROM operations WHERE operation_id=%s",
            (pending.operation_id,),
        ).fetchone()
    assert stored==("QUEUED",1)

    # No worker has claimed the operation. Re-enter Background with a new
    # epoch, without sweeping in between: even the direct claim must reject.
    invoke(dsn,record_primary_activity,actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=2,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    assert claim(dsn) is None
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,pending.operation_id)[0]=="CANCELLED"
    assert resources(dsn,resource)==0


def test_foreground_queued_ephemeral_targets_first_background_only(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn)
    queued=cmd(instance,resource,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,queued)
    with psycopg.connect(dsn,autocommit=True) as conn:
        stored=conn.execute(
            "SELECT status,background_epoch FROM operations WHERE operation_id=%s",
            (queued.operation_id,),
        ).fetchone()
    assert stored==("QUEUED",1)
    # This is still legitimate in Foreground while waiting for its first
    # Background epoch, and the reaper must not cancel it prematurely.
    invoke(dsn,reap_stale_operations)
    assert operation(dsn,queued.operation_id)[0]=="QUEUED"
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    invoke(dsn,record_primary_activity,actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=2,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    assert claim(dsn) is None
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,queued.operation_id)[0]=="CANCELLED"


def test_foreground_queued_ephemeral_can_run_in_its_first_epoch(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn)
    queued=cmd(instance,resource,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,queued)
    invoke(dsn,enter_background,actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))
    lease=claim(dsn)
    assert lease is not None
    assert lease.operation_id==queued.operation_id
    assert lease.background_epoch==1
    invoke(dsn,complete_operation,lease,result_ref="first-background-only")
    assert operation(dsn,queued.operation_id)[0]=="COMPLETED"


def _submit_blocked_completion(pool_executor, dsn, lease):
    pid_queue=Queue(maxsize=1)
    def complete_from_thread():
        with psycopg.connect(dsn,autocommit=True) as conn:
            pid_queue.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            complete_operation(conn,lease,result_ref="must-not-commit")
    future=pool_executor.submit(complete_from_thread)
    return future,pid_queue.get(timeout=5)


def _assert_waiting_for_db_lock(dsn, pid):
    deadline=time.monotonic()+4
    with psycopg.connect(dsn,autocommit=True) as conn:
        while time.monotonic()<deadline:
            row=conn.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                (pid,),
            ).fetchone()
            if row and row[0]=="Lock":
                return
            time.sleep(0.025)
    raise AssertionError("completion did not reach the intended database lock")


def _let_lease_expire(lease):
    # PostgreSQL clock drives the fence; allow a small guard after the
    # recorded lease timestamp for stable concurrent CI execution.
    delay=(lease.lease_until-datetime.now(timezone.utc)).total_seconds()+0.15
    time.sleep(max(0,delay))


def _assert_expired_completion_rolled_back(dsn, lease, resource):
    with psycopg.connect(dsn,autocommit=True) as conn:
        state=conn.execute(
            "SELECT status,result_ref FROM operations WHERE operation_id=%s",
            (lease.operation_id,),
        ).fetchone()
        assert state==("RUNNING",None)
        assert conn.execute(
            """
            SELECT count(*) FROM events
            WHERE event_type='operation.completed.v1'
              AND payload->>'operation_id'=%s
            """,(str(lease.operation_id),),
        ).fetchone()[0]==0
    assert resources(dsn,resource)==1
    assert invoke(dsn,reap_stale_operations)>=1
    assert resources(dsn,resource)==0


def test_completion_cannot_cross_lease_expiry_while_waiting_for_selection(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    queued=cmd(instance,resource)
    enqueue(dsn,user,queued)
    lease=invoke(
        dsn,claim_operation,worker_id="selection-race",
        capabilities=(),lease_seconds=2,
    )
    assert lease.operation_id==queued.operation_id

    with ThreadPoolExecutor(max_workers=1) as workers:
        with psycopg.connect(dsn,autocommit=True) as blocker:
            with blocker.transaction():
                # Simulate a concurrent presence transaction holding the
                # selection lock without changing generation or mode.
                blocker.execute(
                    "SELECT user_id FROM persona_selection WHERE user_id=%s FOR UPDATE",
                    (user,),
                ).fetchone()
                future,pid=_submit_blocked_completion(workers,dsn,lease)
                _assert_waiting_for_db_lock(dsn,pid)
                _let_lease_expire(lease)
        with pytest.raises(StaleOperation):
            future.result(timeout=10)
    _assert_expired_completion_rolled_back(dsn,lease,resource)


def test_completion_cannot_cross_lease_expiry_while_releasing_pool(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    queued=cmd(instance,resource)
    enqueue(dsn,user,queued)
    lease=invoke(
        dsn,claim_operation,worker_id="pool-race",
        capabilities=(),lease_seconds=2,
    )
    assert lease.operation_id==queued.operation_id

    with ThreadPoolExecutor(max_workers=1) as workers:
        with psycopg.connect(dsn,autocommit=True) as blocker:
            with blocker.transaction():
                blocker.execute(
                    """
                    SELECT resource_key FROM execution_resource_pools
                    WHERE resource_key=%s FOR UPDATE
                    """,(resource,),
                ).fetchone()
                future,pid=_submit_blocked_completion(workers,dsn,lease)
                _assert_waiting_for_db_lock(dsn,pid)
                _let_lease_expire(lease)
        with pytest.raises(StaleOperation):
            future.result(timeout=10)
    _assert_expired_completion_rolled_back(dsn,lease,resource)



def test_legacy_unbound_ephemeral_pending_row_is_cancelled_by_reaper(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn)
    queued=cmd(instance,resource,policy=Policy.BACKGROUND_EPHEMERAL)
    enqueue(dsn,user,queued)
    # An earlier P0-04 iteration persisted unbound NULL ephemeral epochs.
    # The recovery sweep must not leave those rows in QUEUED forever.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET background_epoch=NULL WHERE operation_id=%s",
            (queued.operation_id,),
        )
    assert claim(dsn) is None
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,queued.operation_id)[0]=="CANCELLED"
    assert resources(dsn,resource)==0



def test_heartbeat_cannot_revive_expired_lease_after_selection_lock_wait(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    queued=cmd(instance,resource)
    enqueue(dsn,user,queued)
    lease=invoke(
        dsn,claim_operation,worker_id="heartbeat-selection-race",
        capabilities=(),lease_seconds=2,
    )
    assert lease.operation_id==queued.operation_id

    pid_queue=Queue(maxsize=1)
    def delayed_heartbeat():
        with psycopg.connect(dsn,autocommit=True) as conn:
            pid_queue.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            return heartbeat_operation(conn,lease,extend_seconds=60)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with psycopg.connect(dsn,autocommit=True) as blocker:
            with blocker.transaction():
                # Simulate primary activity: an exclusive selection-row lock
                # without changing the selected persona, mode or generation.
                blocker.execute(
                    """
                    UPDATE persona_selection
                    SET presence_revision=presence_revision+1
                    WHERE user_id=%s
                    RETURNING generation,mode
                    """,
                    (user,),
                ).fetchone()
                future=workers.submit(delayed_heartbeat)
                pid=pid_queue.get(timeout=5)
                _assert_waiting_for_db_lock(dsn,pid)
                _let_lease_expire(lease)

        with pytest.raises(StaleOperation):
            future.result(timeout=10)

    # A rejected heartbeat must leave the old lease and claim untouched.
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """
            SELECT status,worker_id,claim_generation,lease_until
            FROM operations WHERE operation_id=%s
            """,
            (queued.operation_id,),
        ).fetchone()
        assert row==("RUNNING",lease.worker_id,lease.claim_generation,lease.lease_until)
        assert conn.execute(
            """
            SELECT count(*) FROM events
            WHERE event_type='operation.claimed.v1'
              AND payload->>'operation_id'=%s
            """,
            (str(queued.operation_id),),
        ).fetchone()[0]==1
    assert resources(dsn,resource)==1
    assert invoke(dsn,reap_stale_operations)>=1
    assert operation(dsn,queued.operation_id)[0]=="RETRY_WAIT"
    assert resources(dsn,resource)==0


def test_heartbeat_still_extends_live_lease(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    queued=cmd(instance,resource)
    enqueue(dsn,user,queued)
    lease=invoke(
        dsn,claim_operation,worker_id="heartbeat-live",
        capabilities=(),lease_seconds=2,
    )
    refreshed=invoke(dsn,heartbeat_operation,lease,extend_seconds=20)
    assert refreshed.operation_id==lease.operation_id
    assert refreshed.claim_generation==lease.claim_generation
    assert refreshed.lease_until > lease.lease_until
    assert resources(dsn,resource)==1
    invoke(dsn,complete_operation,refreshed,result_ref="live-heartbeat-finished")
    assert resources(dsn,resource)==0
