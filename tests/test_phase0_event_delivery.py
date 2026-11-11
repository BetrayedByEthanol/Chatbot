"""P0-03 PostgreSQL delivery, durability and concurrency acceptance tests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus

from persona_agent.contracts import RenameInstanceCommand, TrustedActor
from persona_agent.event_delivery import (
    StaleDelivery, UnknownSubscription, backfill_subscription,
    claim_delivery, defer_delivery, dispatch_outbox, process_delivery,
    reap_expired_claims, resume_deferred, retry_delivery, subscribe,
)
from persona_agent.runtime import PermissionDenied, rename_instance

RENAMED = "persona.instance.renamed.v1"


def seed(dsn):
    user, persona, instance = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            conn.execute(
                "INSERT INTO persona_definitions(id, stable_key) VALUES (%s, %s)",
                (persona, f"p-{persona}"),
            )
            conn.execute(
                """
                INSERT INTO persona_instances(id,user_id,persona_id,label)
                VALUES (%s,%s,%s,'before')
                """,
                (instance,user,persona),
            )
            conn.execute(
                """
                INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                VALUES (%s,%s,1,'FOREGROUND')
                """,
                (user,instance),
            )
    return user,instance


def actor(user):
    return TrustedActor(user_id=user, source_id="test-primary")


def new_consumer():
    return "consumer-" + str(uuid4())


def publish(dsn, user, instance, *, version=0):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return rename_instance(
            conn, actor(user),
            RenameInstanceCommand(
                instance_id=instance, expected_generation=1,
                expected_version=version, new_label=f"label-{version}",
                idempotency_key=str(uuid4()),
            ),
        )


def subscribe_to(dsn, consumer):
    with psycopg.connect(dsn, autocommit=True) as conn:
        subscribe(conn, consumer, RENAMED)


def drain(dsn):
    # One stream can contain many events; strict stream dispatch requires
    # more than one iteration to advance the stream watermark.
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(50):
            if dispatch_outbox(conn, limit=1000) == 0:
                return
    raise AssertionError("outbox did not drain within bounded test iterations")


def get_delivery(dsn, event_id, consumer):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            """
            SELECT status,attempt_count,claim_generation,worker_id,processed_at
            FROM event_deliveries
            WHERE event_id=%s AND consumer_id=%s
            """, (event_id,consumer),
        ).fetchone()


def claim(dsn, consumer, worker="worker"):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return claim_delivery(conn,consumer_id=consumer,worker_id=worker)


def done(dsn, lease, handler=lambda cursor, event: None):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return process_delivery(conn,lease,handler)


def test_fanout_is_independent_idempotent_and_at_least_once(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    a,b=new_consumer(),new_consumer()
    subscribe_to(dsn,a)
    subscribe_to(dsn,b)
    first=publish(dsn,u,instance)
    drain(dsn)
    assert get_delivery(dsn,first.event_id,a)[0] == "PENDING"
    assert get_delivery(dsn,first.event_id,b)[0] == "PENDING"
    drain(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM event_deliveries WHERE event_id=%s",
            (first.event_id,),
        ).fetchone()[0] == 2
    lease_a=claim(dsn,a)
    lease_b=claim(dsn,b)
    assert lease_a.event_id == lease_b.event_id == first.event_id
    envelope=done(dsn,lease_a)
    assert envelope.event_type == RENAMED
    assert envelope.user_id == u and envelope.instance_id == instance
    assert envelope.payload["new_version"] == 1
    assert get_delivery(dsn,first.event_id,a)[0] == "PROCESSED"
    assert get_delivery(dsn,first.event_id,b)[0] == "CLAIMED"
    done(dsn,lease_b)
    assert get_delivery(dsn,first.event_id,b)[0] == "PROCESSED"


def test_outbox_without_subscribers_is_dispatched_without_spin(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    # Subscription registry is durable and shared across integration tests.
    # Temporarily disable only this event type, then restore exact prior state.
    with psycopg.connect(dsn,autocommit=True) as conn:
        enabled = [
            row[0] for row in conn.execute(
                "SELECT consumer_id FROM event_subscriptions WHERE event_type=%s AND enabled",
                (RENAMED,),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE event_subscriptions SET enabled=false WHERE event_type=%s",
            (RENAMED,),
        )
    try:
        first=publish(dsn,u,instance)
        drain(dsn)
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute(
                "SELECT dispatched_at IS NOT NULL FROM outbox WHERE event_id=%s",
                (first.event_id,),
            ).fetchone() == (True,)
            assert conn.execute(
                "SELECT count(*) FROM event_deliveries WHERE event_id=%s",
                (first.event_id,),
            ).fetchone()[0] == 0
    finally:
        if enabled:
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute(
                    """
                    UPDATE event_subscriptions SET enabled=true
                    WHERE event_type=%s AND consumer_id=ANY(%s)
                    """,
                    (RENAMED,enabled),
                )


def test_only_one_worker_claims_a_delivery(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    first=publish(dsn,u,instance)
    drain(dsn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        x=pool.submit(claim,dsn,consumer,"worker-a")
        y=pool.submit(claim,dsn,consumer,"worker-b")
        claims=[x.result(),y.result()]
    assert sum(x is not None for x in claims) == 1
    lease=next(x for x in claims if x is not None)
    assert lease.event_id == first.event_id
    assert lease.attempt_count == lease.claim_generation == 1
    done(dsn,lease)
    assert claim(dsn,consumer) is None


def test_ordered_stream_does_not_overtake_pending_or_failed_predecessor(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    first=publish(dsn,u,instance,version=0)
    second=publish(dsn,u,instance,version=1)
    drain(dsn)
    lease=claim(dsn,consumer)
    assert lease.event_id == first.event_id
    assert claim(dsn,consumer,worker="worker-other") is None
    done(dsn,lease)
    next_lease=claim(dsn,consumer)
    assert next_lease.event_id == second.event_id
    done(dsn,next_lease)


def test_claim_expiry_invalidates_old_worker_and_reclaims(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    event=publish(dsn,u,instance)
    drain(dsn)
    old=claim(dsn,consumer,"old")
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """
            UPDATE event_deliveries
            SET lease_until=clock_timestamp()-interval '10 seconds'
            WHERE delivery_id=%s
            """,(old.delivery_id,),
        )
    new=claim(dsn,consumer,"new")
    assert new.event_id == event.event_id
    assert new.claim_generation == old.claim_generation + 1
    with pytest.raises(StaleDelivery):
        done(dsn,old)
    done(dsn,new)
    assert get_delivery(dsn,event.event_id,consumer)[0] == "PROCESSED"


def test_handler_and_ack_are_one_atomic_transaction(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    event=publish(dsn,u,instance)
    drain(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS integration_delivery_effects (
                event_id uuid PRIMARY KEY,
                applied text NOT NULL
            )
            """,
        )
    first_claim=claim(dsn,consumer)
    def broken_handler(cursor,envelope):
        cursor.execute(
            """
            INSERT INTO integration_delivery_effects(event_id,applied)
            VALUES (%s,'uncommitted')
            """, (envelope.event_id,),
        )
        raise RuntimeError("database-only handler failed")

    with pytest.raises(RuntimeError,match="handler failed"):
        done(dsn,first_claim,broken_handler)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM integration_delivery_effects WHERE event_id=%s",
            (event.event_id,),
        ).fetchone()[0] == 0
        assert get_delivery(dsn,event.event_id,consumer)[0] == "CLAIMED"
        assert retry_delivery(conn,first_claim,error="retry me",delay_seconds=0) == "RETRY_PENDING"
    next_claim=claim(dsn,consumer)
    def successful_handler(cursor,envelope):
        cursor.execute(
            """
            INSERT INTO integration_delivery_effects(event_id,applied)
            VALUES (%s,'applied')
            """, (envelope.event_id,),
        )
    done(dsn,next_claim,successful_handler)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT applied FROM integration_delivery_effects WHERE event_id=%s",
            (event.event_id,),
        ).fetchone() == ("applied",)
    with pytest.raises(StaleDelivery):
        done(dsn,next_claim)
    assert get_delivery(dsn,event.event_id,consumer)[0] == "PROCESSED"


def test_retry_budget_exhaustion_and_failed_predecessor_blocks_next(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    first=publish(dsn,u,instance)
    second=publish(dsn,u,instance,version=1)
    drain(dsn)
    for attempt in range(1,4):
        lease=claim(dsn,consumer)
        assert lease.event_id == first.event_id and lease.attempt_count == attempt
        with psycopg.connect(dsn,autocommit=True) as conn:
            status=retry_delivery(conn,lease,error="permanent",delay_seconds=0)
        assert status == ("FAILED" if attempt == 3 else "RETRY_PENDING")
    assert claim(dsn,consumer) is None
    assert get_delivery(dsn,first.event_id,consumer)[0] == "FAILED"
    assert get_delivery(dsn,second.event_id,consumer)[0] == "PENDING"


def test_expired_lease_reaper_releases_and_fences(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    event=publish(dsn,u,instance)
    drain(dsn)
    old=claim(dsn,consumer)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """
            UPDATE event_deliveries SET lease_until=clock_timestamp()-interval '1 second'
            WHERE delivery_id=%s
            """,(old.delivery_id,),
        )
        assert reap_expired_claims(conn,limit=1000) >= 1
    assert get_delivery(dsn,event.event_id,consumer)[0] == "RETRY_PENDING"
    with pytest.raises(StaleDelivery):
        done(dsn,old)
    again=claim(dsn,consumer)
    assert again.claim_generation > old.claim_generation
    done(dsn,again)


def test_deferred_requires_resume_and_old_claim_cannot_ack(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    event=publish(dsn,u,instance)
    drain(dsn)
    lease=claim(dsn,consumer)
    with psycopg.connect(dsn,autocommit=True) as conn:
        defer_delivery(conn,lease)
    assert get_delivery(dsn,event.event_id,consumer)[0] == "DEFERRED"
    assert claim(dsn,consumer) is None
    with pytest.raises(StaleDelivery):
        done(dsn,lease)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert not resume_deferred(conn,consumer_id="unknown-consumer",delivery_id=lease.delivery_id)
        assert resume_deferred(conn,consumer_id=consumer,delivery_id=lease.delivery_id)
        assert not resume_deferred(conn,consumer_id=consumer,delivery_id=lease.delivery_id)
    new=claim(dsn,consumer)
    done(dsn,new)


def test_dispatcher_does_not_overtake_locked_outbox_row(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    first=publish(dsn,u,instance)
    second=publish(dsn,u,instance,version=1)
    with psycopg.connect(dsn,autocommit=True) as holder:
        with holder.transaction():
            holder.execute(
                "SELECT outbox_id FROM outbox WHERE event_id=%s FOR UPDATE",
                (first.event_id,),
            ).fetchone()
            with psycopg.connect(dsn,autocommit=True) as other:
                dispatch_outbox(other,limit=1000)
                assert other.execute(
                    "SELECT dispatched_at FROM outbox WHERE event_id=%s",
                    (second.event_id,),
                ).fetchone()[0] is None
    drain(dsn)
    assert get_delivery(dsn,first.event_id,consumer)[0] == "PENDING"
    assert get_delivery(dsn,second.event_id,consumer)[0] == "PENDING"


def test_late_subscriber_backfill_is_scoped_and_idempotent(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    event=publish(dsn,u,instance)
    drain(dsn)  # no subscribers: already dispatched
    consumer=new_consumer()
    subscribe_to(dsn,consumer)
    stranger, _=seed(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(PermissionDenied):
            backfill_subscription(
                conn,actor(stranger),instance_id=instance,
                consumer_id=consumer,event_type=RENAMED,
            )
        with pytest.raises(UnknownSubscription):
            backfill_subscription(
                conn,actor(u),instance_id=instance,
                consumer_id=consumer,event_type="not.subscribed",
            )
        assert backfill_subscription(
            conn,actor(u),instance_id=instance,
            consumer_id=consumer,event_type=RENAMED,
        ) == 1
        assert backfill_subscription(
            conn,actor(u),instance_id=instance,
            consumer_id=consumer,event_type=RENAMED,
        ) == 0
    assert get_delivery(dsn,event.event_id,consumer)[0] == "PENDING"
    done(dsn,claim(dsn,consumer))


def test_delivery_functions_reject_nested_transactions(dsn):
    drain(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            assert conn.info.transaction_status == TransactionStatus.INTRANS
            with pytest.raises(ValueError,match="idle autocommit"):
                subscribe(conn,new_consumer(),RENAMED)
            with pytest.raises(ValueError,match="idle autocommit"):
                dispatch_outbox(conn)
            with pytest.raises(ValueError,match="idle autocommit"):
                claim_delivery(conn,consumer_id="x",worker_id="y")


def test_consumer_registration_rejects_blank_values(dsn):
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(ValueError):
            subscribe(conn,"",RENAMED)
        with pytest.raises(ValueError):
            subscribe(conn,"x","")


def test_second_consumer_not_blocked_by_failed_first_consumer(dsn):
    drain(dsn)
    u,instance=seed(dsn)
    a,b=new_consumer(),new_consumer()
    subscribe_to(dsn,a)
    subscribe_to(dsn,b)
    first=publish(dsn,u,instance)
    drain(dsn)
    claim_a=claim(dsn,a)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE event_deliveries SET max_attempts=1 WHERE delivery_id=%s",
            (claim_a.delivery_id,),
        )
        assert retry_delivery(conn,claim_a,error="fail",delay_seconds=0) == "FAILED"
    claim_b=claim(dsn,b)
    assert claim_b.event_id == first.event_id
    done(dsn,claim_b)
    assert get_delivery(dsn,first.event_id,a)[0] == "FAILED"
    assert get_delivery(dsn,first.event_id,b)[0] == "PROCESSED"



def test_late_subscriber_backfill_pages_across_many_sequence_one_streams(dsn):
    """A page must exclude existing deliveries before LIMIT, even >1,000."""
    from uuid import NAMESPACE_URL, uuid5

    # An operation has a dedicated event stream, so each queued event is
    # sequence 1. Synthetic journal rows keep this test fast while retaining
    # the real event-stream, outbox, and delivery foreign-key constraints.
    user, instance=seed(dsn)
    count=1005
    operation_ids=[uuid4() for _ in range(count)]
    stream_ids=[
        uuid5(NAMESPACE_URL,f"persona-agent/operation/{operation_id}")
        for operation_id in operation_ids
    ]
    event_ids=[uuid4() for _ in range(count)]
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO event_streams(
                    stream_id,user_id,instance_id,domain,last_sequence
                )
                SELECT row.stream_id,%s,%s,'operation:'||row.operation_id::text,1
                FROM unnest(%s::uuid[],%s::uuid[]) AS row(stream_id,operation_id)
                """,
                (user,instance,stream_ids,operation_ids),
            )
            conn.execute(
                """
                INSERT INTO events(
                    event_id,event_type,schema_version,user_id,instance_id,
                    stream_id,stream_sequence,source_id,trust_level,payload
                )
                SELECT row.event_id,'operation.queued.v1',1,%s,%s,
                       row.stream_id,1,'operation.scheduler','TRUSTED_PLATFORM',
                       jsonb_build_object('operation_id',row.operation_id::text)
                FROM unnest(
                    %s::uuid[],%s::uuid[],%s::uuid[]
                ) AS row(event_id,stream_id,operation_id)
                """,
                (user,instance,event_ids,stream_ids,operation_ids),
            )
            # The subscriber arrives after these events were dispatched.
            conn.execute(
                """
                INSERT INTO outbox(outbox_id,event_id,dispatched_at)
                SELECT gen_random_uuid(), event_id, clock_timestamp()
                FROM events WHERE event_id=ANY(%s::uuid[])
                """,
                (event_ids,),
            )
    consumer=new_consumer()
    with psycopg.connect(dsn,autocommit=True) as conn:
        subscribe(conn,consumer,"operation.queued.v1")
        pages=[]
        for _ in range(15):
            n=backfill_subscription(
                conn,actor(user),instance_id=instance,consumer_id=consumer,
                event_type="operation.queued.v1",limit=100,
            )
            pages.append(n)
            if not n:
                break
        assert pages==[100]*10+[5,0]
        assert backfill_subscription(
            conn,actor(user),instance_id=instance,consumer_id=consumer,
            event_type="operation.queued.v1",limit=1000,
        )==0
        assert conn.execute(
            """
            SELECT count(*),count(DISTINCT e.stream_id),
                   min(e.stream_sequence),max(e.stream_sequence)
            FROM event_deliveries d JOIN events e ON e.event_id=d.event_id
            WHERE d.consumer_id=%s AND e.event_type='operation.queued.v1'
            """,(consumer,),
        ).fetchone()==(1005,1005,1,1)
        # The stream-local sequence filter is still honored.
        assert backfill_subscription(
            conn,actor(user),instance_id=instance,consumer_id=consumer,
            event_type="operation.queued.v1",after_sequence=1,
        )==0
