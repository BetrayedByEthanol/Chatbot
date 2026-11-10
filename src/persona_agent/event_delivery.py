"""PostgreSQL outbox, at-least-once delivery, and fenced DB-only consumers.

Caller boundary: these service functions are internal, not tool-call endpoints.
Never run network calls, model inference, or non-transactional side effects in
process_delivery's database-only handler.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .runtime import PermissionDenied


@dataclass(frozen=True)
class DeliveryLease:
    delivery_id: UUID
    event_id: UUID
    consumer_id: str
    worker_id: str
    claim_generation: int
    attempt_count: int
    lease_until: datetime


@dataclass(frozen=True)
class EventEnvelope:
    event_id: UUID
    event_type: str
    schema_version: int
    user_id: UUID
    instance_id: UUID
    stream_id: UUID
    stream_sequence: int
    source_id: str
    trust_level: str
    payload: dict | None
    payload_ref: str | None


class StaleDelivery(Exception):
    pass


class UnknownSubscription(Exception):
    pass


def _boundary(connection: Connection) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("delivery commands require an idle autocommit connection")


def _valid_token(token: str) -> None:
    if not 1 <= len(token) <= 128:
        raise ValueError("consumer and worker identifiers must contain 1–128 characters")


def subscribe(connection: Connection, consumer_id: str, event_type: str) -> None:
    """Trusted bootstrap only. Registration affects *future* outbox dispatch."""
    _boundary(connection)
    _valid_token(consumer_id)
    if not 1 <= len(event_type) <= 200:
        raise ValueError("event_type must contain 1–200 characters")
    with connection.transaction():
        connection.execute(
            "INSERT INTO event_consumers(consumer_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (consumer_id,),
        )
        connection.execute(
            """
            INSERT INTO event_subscriptions(consumer_id, event_type, enabled)
            VALUES (%s, %s, true)
            ON CONFLICT (consumer_id, event_type) DO UPDATE SET enabled = true
            """,
            (consumer_id, event_type),
        )


def dispatch_outbox(connection: Connection, *, limit: int = 100) -> int:
    """Atomically fan out journal events and mark their outbox rows dispatched.

    Dispatchers contend through SKIP LOCKED; older events in the same stream
    must be dispatched first, even if another dispatcher holds their row lock.
    No subscribers is a valid empty fanout, not a reason to spin.
    """
    _boundary(connection)
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT o.outbox_id, o.event_id, e.event_type
                FROM outbox o JOIN events e ON e.event_id = o.event_id
                WHERE o.dispatched_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM outbox prior_o
                      JOIN events prior_e ON prior_e.event_id = prior_o.event_id
                      WHERE prior_e.stream_id = e.stream_id
                        AND prior_e.stream_sequence < e.stream_sequence
                        AND prior_o.dispatched_at IS NULL
                  )
                ORDER BY o.created_at, o.outbox_id
                LIMIT %s
                FOR UPDATE OF o SKIP LOCKED
                """,
                (limit,),
            )
            selected = cursor.fetchall()
            for row in selected:
                cursor.execute(
                    """
                    SELECT consumer_id FROM event_subscriptions
                    WHERE event_type = %s AND enabled = true
                    ORDER BY consumer_id
                    """,
                    (row["event_type"],),
                )
                for subscription in cursor.fetchall():
                    cursor.execute(
                        """
                        INSERT INTO event_deliveries(delivery_id, event_id, consumer_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (event_id, consumer_id) DO NOTHING
                        """,
                        (uuid4(), row["event_id"], subscription["consumer_id"]),
                    )
                cursor.execute(
                    """
                    UPDATE outbox SET dispatched_at = clock_timestamp()
                    WHERE outbox_id = %s
                    """,
                    (row["outbox_id"],),
                )
            return len(selected)


def claim_delivery(
    connection: Connection,
    *,
    consumer_id: str,
    worker_id: str,
    lease_seconds: int = 30,
) -> DeliveryLease | None:
    """Take one independently fenced delivery; preserves per-stream consumer order."""
    _boundary(connection)
    _valid_token(consumer_id)
    _valid_token(worker_id)
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be 1..3600")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT d.delivery_id
                FROM event_deliveries d
                JOIN events e ON e.event_id = d.event_id
                WHERE d.consumer_id = %s
                  AND (
                      (d.status IN ('PENDING', 'RETRY_PENDING')
                       AND d.eligible_at <= clock_timestamp())
                      OR (d.status = 'CLAIMED'
                          AND d.lease_until <= clock_timestamp()
                          AND d.attempt_count < d.max_attempts)
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM event_deliveries earlier
                      JOIN events prior_e ON prior_e.event_id = earlier.event_id
                      WHERE earlier.consumer_id = d.consumer_id
                        AND prior_e.stream_id = e.stream_id
                        AND prior_e.stream_sequence < e.stream_sequence
                        AND earlier.status <> 'PROCESSED'
                  )
                ORDER BY e.recorded_at, e.stream_sequence, d.delivery_id
                LIMIT 1
                FOR UPDATE OF d SKIP LOCKED
                """,
                (consumer_id,),
            )
            chosen = cursor.fetchone()
            if chosen is None:
                return None
            cursor.execute(
                """
                UPDATE event_deliveries
                SET status = 'CLAIMED', worker_id = %s,
                    lease_until = clock_timestamp() + (%s * interval '1 second'),
                    claim_generation = claim_generation + 1,
                    attempt_count = attempt_count + 1,
                    updated_at = clock_timestamp()
                WHERE delivery_id = %s
                RETURNING delivery_id, event_id, consumer_id, worker_id,
                          claim_generation, attempt_count, lease_until
                """,
                (worker_id, lease_seconds, chosen["delivery_id"]),
            )
            return DeliveryLease(**cursor.fetchone())


def _assert_claim(row: dict | None, lease: DeliveryLease) -> None:
    if (
        row is None or row["status"] != "CLAIMED"
        or row["consumer_id"] != lease.consumer_id
        or row["worker_id"] != lease.worker_id
        or row["claim_generation"] != lease.claim_generation
        or row["lease_until"] <= row["database_now"]
    ):
        raise StaleDelivery("delivery lease expired, changed, or already completed")


def _locked_claim(cursor, lease: DeliveryLease) -> dict:
    cursor.execute(
        """
        SELECT d.status, d.consumer_id, d.worker_id, d.claim_generation,
               d.lease_until, d.attempt_count, d.max_attempts,
               clock_timestamp() AS database_now
        FROM event_deliveries d WHERE d.delivery_id = %s
        FOR UPDATE
        """,
        (lease.delivery_id,),
    )
    row = cursor.fetchone()
    _assert_claim(row, lease)
    return row


def process_delivery(
    connection: Connection,
    lease: DeliveryLease,
    handler: Callable,
) -> EventEnvelope:
    """Run a DATABASE-ONLY handler and its acknowledgment in one transaction.

    Handler signature: handler(cursor, event_envelope). It may issue SQL through
    the supplied cursor, including domain commits/outbox inserts, but must not
    perform model inference, network calls or external effects. If it raises
    or the lease expires, both its SQL and the ACK roll back.
    """
    _boundary(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            _locked_claim(cursor, lease)
            cursor.execute(
                """
                SELECT e.event_id, e.event_type, e.schema_version,
                       e.user_id, e.instance_id, e.stream_id,
                       e.stream_sequence, e.source_id, e.trust_level,
                       e.payload, e.payload_ref
                FROM event_deliveries d
                JOIN events e ON e.event_id = d.event_id
                WHERE d.delivery_id = %s
                """,
                (lease.delivery_id,),
            )
            payload = cursor.fetchone()
            if payload is None:
                raise StaleDelivery("event for delivery no longer exists")
            envelope = EventEnvelope(**payload)
            handler(cursor, envelope)
            cursor.execute(
                """
                UPDATE event_deliveries
                SET status = 'PROCESSED', worker_id = NULL, lease_until = NULL,
                    processed_at = clock_timestamp(), updated_at = clock_timestamp(),
                    last_error = NULL
                WHERE delivery_id = %s AND consumer_id = %s
                  AND status = 'CLAIMED' AND worker_id = %s
                  AND claim_generation = %s AND lease_until > clock_timestamp()
                RETURNING delivery_id
                """,
                (
                    lease.delivery_id, lease.consumer_id,
                    lease.worker_id, lease.claim_generation,
                ),
            )
            if cursor.fetchone() is None:
                raise StaleDelivery("lease expired during database handler")
            return envelope


def retry_delivery(
    connection: Connection,
    lease: DeliveryLease,
    *,
    error: str,
    delay_seconds: int = 1,
) -> str:
    """Release a failed DB delivery with bounded retry, or exhaust it."""
    _boundary(connection)
    if not 0 <= delay_seconds <= 3600:
        raise ValueError("delay_seconds must be 0..3600")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row = _locked_claim(cursor, lease)
            new_status = "FAILED" if row["attempt_count"] >= row["max_attempts"] else "RETRY_PENDING"
            cursor.execute(
                """
                UPDATE event_deliveries
                SET status = %s, worker_id = NULL, lease_until = NULL,
                    eligible_at = clock_timestamp() + (%s * interval '1 second'),
                    updated_at = clock_timestamp(), last_error = %s
                WHERE delivery_id = %s
                """,
                (new_status, delay_seconds, error[:1000], lease.delivery_id),
            )
            return new_status


def defer_delivery(connection: Connection, lease: DeliveryLease) -> None:
    """Park a claimed item (e.g., inactive persona) without acknowledging it."""
    _boundary(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            _locked_claim(cursor, lease)
            cursor.execute(
                """
                UPDATE event_deliveries
                SET status = 'DEFERRED', worker_id = NULL, lease_until = NULL,
                    updated_at = clock_timestamp()
                WHERE delivery_id = %s
                """,
                (lease.delivery_id,),
            )


def resume_deferred(connection: Connection, *, consumer_id: str, delivery_id: UUID) -> bool:
    """Internal scheduler action after the owning domain confirms eligibility."""
    _boundary(connection)
    _valid_token(consumer_id)
    with connection.transaction():
        cursor = connection.execute(
            """
            UPDATE event_deliveries
            SET status = 'PENDING', eligible_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE delivery_id = %s AND consumer_id = %s AND status = 'DEFERRED'
            RETURNING delivery_id
            """,
            (delivery_id, consumer_id),
        )
        return cursor.fetchone() is not None


def reap_expired_claims(connection: Connection, *, limit: int = 100) -> int:
    """Release expired leases; after max attempts park as FAILED for review."""
    _boundary(connection)
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT delivery_id FROM event_deliveries
                    WHERE status = 'CLAIMED' AND lease_until <= clock_timestamp()
                    ORDER BY lease_until, delivery_id
                    LIMIT %s FOR UPDATE SKIP LOCKED
                )
                UPDATE event_deliveries d
                SET status = CASE WHEN d.attempt_count >= d.max_attempts
                                  THEN 'FAILED' ELSE 'RETRY_PENDING' END,
                    worker_id = NULL, lease_until = NULL,
                    eligible_at = clock_timestamp(),
                    last_error = 'delivery lease expired; outcome requires reconciliation',
                    updated_at = clock_timestamp()
                FROM expired WHERE d.delivery_id = expired.delivery_id
                """,
                (limit,),
            )
            return cursor.rowcount


def backfill_subscription(
    connection: Connection,
    actor: TrustedActor,
    *,
    instance_id: UUID,
    consumer_id: str,
    event_type: str,
    after_sequence: int = 0,
    limit: int = 100,
) -> int:
    """Explicit, idempotent recovery for events missed by late subscriptions.

    Only missing rows are inserted. Existing PROCESSED deliveries are never reset.
    Caller must quiesce the consumer when historical order matters.
    """
    _boundary(connection)
    _valid_token(consumer_id)
    if after_sequence < 0 or not 1 <= limit <= 1000:
        raise ValueError("invalid replay range")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT 1 FROM persona_instances
                WHERE id = %s AND user_id = %s
                """,
                (instance_id, actor.user_id),
            )
            if cursor.fetchone() is None:
                raise PermissionDenied("replay instance outside authenticated scope")
            cursor.execute(
                """
                SELECT 1 FROM event_subscriptions
                WHERE consumer_id = %s AND event_type = %s AND enabled = true
                """,
                (consumer_id, event_type),
            )
            if cursor.fetchone() is None:
                raise UnknownSubscription("consumer not subscribed to event type")
            cursor.execute(
                """
                SELECT event_id FROM events
                WHERE user_id = %s AND instance_id = %s AND event_type = %s
                  AND stream_sequence > %s
                ORDER BY recorded_at, stream_id, stream_sequence, event_id
                LIMIT %s
                """,
                (actor.user_id, instance_id, event_type, after_sequence, limit),
            )
            inserted = 0
            for event in cursor.fetchall():
                cursor.execute(
                    """
                    INSERT INTO event_deliveries(delivery_id, event_id, consumer_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (event_id, consumer_id) DO NOTHING
                    RETURNING delivery_id
                    """,
                    (uuid4(), event["event_id"], consumer_id),
                )
                inserted += int(cursor.fetchone() is not None)
            return inserted
