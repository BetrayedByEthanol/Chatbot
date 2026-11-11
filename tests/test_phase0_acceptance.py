"""P0-05: cross-domain Phase 0 acceptance of real PostgreSQL contracts.

No model/tool worker loop is introduced: command admission and event handlers
are deliberately separate trusted transaction boundaries.
"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.contracts import RenameInstanceCommand, TrustedActor
from persona_agent.event_delivery import (
    StaleDelivery, claim_delivery, dispatch_outbox, process_delivery, subscribe,
)
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.operation_contracts import (
    EffectClass, EnqueueOperation, Priority, StaleOperation, Status,
)
from persona_agent.operation_scheduler import (
    claim_operation, configure_resource_pool, enqueue_operation,
)
from persona_agent.operation_control import (
    checkpoint_operation, complete_operation, mark_external_dispatched,
    reap_stale_operations, reconcile_unknown, resume_suspended,
)
from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, rename_instance,
)


@pytest.fixture(autouse=True)
def clean_shared_queue(dsn):
    # This is a disposable integration database. Other Phase 0 tests also
    # leave completed or suspended audit rows across independent cases.
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM operation_concurrency_locks")
            conn.execute("DELETE FROM operation_resource_reservations")
            conn.execute(
                "UPDATE execution_resource_pools "
                "SET active_units=0, active_non_p0_units=0"
            )
            conn.execute(
                """UPDATE operations SET status='CANCELLED',worker_id=NULL,
                   lease_until=NULL,claimed_at=NULL
                   WHERE status IN (
                     'QUEUED','RUNNING','RETRY_WAIT','CANCEL_REQUESTED',
                     'SUSPEND_REQUESTED','SUSPENDED','BLOCKED'
                   )"""
            )


def call(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return fn(conn, *args, **kwargs)


def seed(dsn, *, two=False):
    user, persona, instance = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona, f"p05-{persona}"),
            )
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES (%s,%s,%s,'initial')",
                (instance, user, persona),
            )
            if two:
                second_persona, other = uuid4(), uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (second_persona, f"p05-{second_persona}"),
                )
                conn.execute(
                    "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                    "VALUES (%s,%s,%s,'other')",
                    (other, user, second_persona),
                )
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')",
                (user, instance),
            )
    return (user, instance, other) if two else (user, instance)


def actor(user):
    return TrustedActor(user_id=user, source_id="p05-authenticated")


def command(instance, resource, *, generation=1, effect=EffectClass.NONE,
            versions=None):
    return EnqueueOperation(
        operation_id=uuid4(), instance_id=instance,
        expected_generation=generation,
        idempotency_key=f"p05-{uuid4()}",
        operation_type="p05.acceptance", priority_class=Priority.P1,
        effect_class=effect, resources={resource: 1},
        input_versions=versions or {},
    )


def operation_claim(dsn, *, worker="p05-worker"):
    return call(
        dsn, claim_operation, worker_id=worker,
        capabilities=(), lease_seconds=60,
    )


def pool(dsn, capacity=2):
    key=f"p05-pool-{uuid4()}"
    call(dsn, configure_resource_pool, key, capacity_units=capacity)
    return key


def state(dsn, operation_id):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT status,effect_status,checkpoint_ref,result_ref "
            "FROM operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()


def drain_outbox(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(500):
            if dispatch_outbox(conn, limit=1000)==0:
                return
    raise AssertionError("Phase 0 outbox could not drain")


@pytest.fixture
def rename_consumer(dsn):
    # Reproduce the shared-database backlog deterministically: another
    # command committed a rename event before this consumer exists.
    earlier_user, earlier_instance=seed(dsn)
    earlier=call(dsn,rename_instance,actor(earlier_user),RenameInstanceCommand(
        instance_id=earlier_instance, expected_generation=1,
        expected_version=0, new_label="earlier",
        idempotency_key=f"earlier-rename-{uuid4()}",
    ))
    # Outbox fanout follows *dispatch* time, not event creation time.
    # Drain all earlier events before registration so a new subscription
    # observes only subsequently published events.
    drain_outbox(dsn)
    consumer=f"p05-consumer-{uuid4()}"
    call(dsn,subscribe,consumer,"persona.instance.renamed.v1")
    try:
        yield consumer, earlier.event_id
    finally:
        # Other Phase 0 suites share the disposable test database and
        # assert an exact subscriber count. Do not leak our subscription.
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(
                "UPDATE event_subscriptions SET enabled=false "
                "WHERE consumer_id=%s AND event_type=%s",
                (consumer,"persona.instance.renamed.v1"),
            )


def test_command_event_delivery_operation_checkpoint_completion(dsn, rename_consumer):
    user, instance=seed(dsn)
    resource=pool(dsn)
    consumer, earlier_event_id=rename_consumer
    rename_cmd=RenameInstanceCommand(
        instance_id=instance, expected_generation=1,
        expected_version=0, new_label="accepted",
        idempotency_key=f"rename-{uuid4()}",
    )
    renamed=call(dsn,rename_instance,actor(user),rename_cmd)
    replay=call(dsn,rename_instance,actor(user),rename_cmd)
    assert replay.replayed and replay.event_id==renamed.event_id
    assert renamed.new_version==1
    drain_outbox(dsn)
    delivery=call(
        dsn,claim_delivery,consumer_id=consumer,worker_id="p05-delivery",
    )
    assert delivery is not None and delivery.event_id==renamed.event_id
    # The pre-registration rename was dispatched before subscription, so
    # no delivery should exist for this consumer, regardless of test order.
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM event_deliveries "
            "WHERE consumer_id=%s AND event_id=%s",
            (consumer,earlier_event_id),
        ).fetchone()[0]==0

    def check_event(cursor, envelope):
        assert envelope.user_id==user and envelope.instance_id==instance
        assert envelope.event_type=="persona.instance.renamed.v1"
        assert envelope.payload["new_version"]==1
        assert envelope.source_id=="p05-authenticated"
        cursor.execute(
            "SELECT version,label FROM persona_instances WHERE id=%s",
            (instance,),
        )
        assert tuple(cursor.fetchone().values())==(1,"accepted")

    delivered=call(dsn,process_delivery,delivery,check_event)
    assert delivered.event_id==renamed.event_id
    with pytest.raises(StaleDelivery):
        call(dsn,process_delivery,delivery,check_event)

    # The user explicitly admits a follow-up operation after delivery;
    # delivery handlers must not bypass the trusted enqueue boundary.
    admitted=command(instance,resource,versions={"persona_instance":1})
    queued=call(dsn,enqueue_operation,actor(user),admitted)
    repeat=call(dsn,enqueue_operation,actor(user),admitted)
    assert repeat.replayed and repeat.event_id==queued.event_id
    lease=operation_claim(dsn)
    assert lease.operation_id==admitted.operation_id
    call(dsn,checkpoint_operation,lease,checkpoint_ref="p05-checkpoint://one")
    call(dsn,complete_operation,lease,result_ref="p05-result://done")
    assert state(dsn,admitted.operation_id)==(
        "COMPLETED","CONFIRMED","p05-checkpoint://one","p05-result://done",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT active_units FROM execution_resource_pools WHERE resource_key=%s",
            (resource,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='operation.queued.v1' "
            "AND instance_id=%s AND payload->>'operation_id'=%s",
            (instance,str(admitted.operation_id)),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM command_receipts WHERE user_id=%s "
            "AND instance_id=%s AND idempotency_key=%s",
            (user,instance,admitted.idempotency_key),
        ).fetchone()[0]==1


def test_persona_switch_fences_work_and_explicit_resume_preserves_scope(dsn):
    user,first,second=seed(dsn,two=True)
    other_user,other_instance=seed(dsn)
    resource=pool(dsn)
    request=command(first,resource)
    call(dsn,enqueue_operation,actor(user),request)
    running=operation_claim(dsn)
    assert running.operation_id==request.operation_id

    switched=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=first,target_instance_id=second,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert switched.generation==2
    with pytest.raises(StaleOperation):
        call(dsn,complete_operation,running,result_ref="stale")
    with pytest.raises(PermissionDenied):
        call(dsn,resume_suspended,actor(other_user),operation_id=request.operation_id)
    assert call(dsn,reap_stale_operations)>=1
    assert state(dsn,request.operation_id)[0]=="SUSPENDED"
    with pytest.raises(StaleOperation):
        call(dsn,resume_suspended,actor(user),operation_id=request.operation_id)
    with pytest.raises(StaleSelection):
        call(dsn,enqueue_operation,actor(user),command(first,resource))

    switched_back=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=second,target_instance_id=first,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    assert switched_back.generation==3
    call(dsn,resume_suspended,actor(user),operation_id=request.operation_id)
    resumed=operation_claim(dsn,worker="p05-after-switch")
    assert resumed.operation_id==request.operation_id
    assert resumed.selection_generation==3
    assert resumed.claim_generation==running.claim_generation+1
    call(dsn,complete_operation,resumed,result_ref="authorized")
    assert state(dsn,request.operation_id)[0]=="COMPLETED"
    assert other_instance!=first


def test_idempotency_scope_and_competing_workers(dsn):
    user,instance=seed(dsn)
    other_user,_=seed(dsn)
    resource=pool(dsn,capacity=1)
    requested=command(instance,resource)
    first=call(dsn,enqueue_operation,actor(user),requested)
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,actor(other_user),requested)
    with pytest.raises(IdempotencyConflict):
        call(dsn,enqueue_operation,actor(user),
             requested.model_copy(update={"operation_type":"different"}))
    assert call(dsn,enqueue_operation,actor(user),requested).replayed

    def claim(worker):
        return operation_claim(dsn,worker=worker)

    with ThreadPoolExecutor(max_workers=2) as pool_executor:
        results=list(pool_executor.map(claim,("one","two")))
    winners=[lease for lease in results if lease and lease.operation_id==requested.operation_id]
    assert len(winners)==1
    call(dsn,complete_operation,winners[0],result_ref="winner")
    with pytest.raises(StaleOperation):
        call(dsn,complete_operation,winners[0],result_ref="loser")
    assert state(dsn,requested.operation_id)[0]=="COMPLETED"
    assert first.operation_id==requested.operation_id


def test_recovery_retry_and_external_unknown_fencing(dsn):
    user,instance=seed(dsn)
    resource=pool(dsn,capacity=1)
    internal=command(instance,resource)
    call(dsn,enqueue_operation,actor(user),internal)
    original=operation_claim(dsn,worker="crashed-internal")
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",(internal.operation_id,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert state(dsn,internal.operation_id)[0]=="RETRY_WAIT"
    with pytest.raises(StaleOperation):
        call(dsn,checkpoint_operation,original,checkpoint_ref="late")
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp() "
            "WHERE operation_id=%s",(internal.operation_id,),
        )
    resumed=operation_claim(dsn,worker="recovered-internal")
    assert resumed.claim_generation==original.claim_generation+1
    call(dsn,complete_operation,resumed,result_ref="retried")

    external=command(instance,resource,effect=EffectClass.EXTERNAL)
    call(dsn,enqueue_operation,actor(user),external)
    claimed=operation_claim(dsn,worker="crashed-external")
    call(dsn,mark_external_dispatched,claimed)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",(external.operation_id,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert state(dsn,external.operation_id)[0]=="OUTCOME_UNKNOWN"
    assert operation_claim(dsn) is None
    assert call(
        dsn,reconcile_unknown,
        TrustedPlatformActor(user_id=user,source_id="p05-reconciliation"),
        operation_id=external.operation_id,confirmed=True,
        evidence_ref="provider://accepted",
    )==Status.COMPLETED
    assert state(dsn,external.operation_id)[0]=="COMPLETED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT active_units FROM execution_resource_pools WHERE resource_key=%s",
            (resource,),
        ).fetchone()[0]==0
