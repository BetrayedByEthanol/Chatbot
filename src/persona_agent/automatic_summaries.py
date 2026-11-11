"""P2-03: trusted, bounded event-driven admission of P2-02 summary operations.

Only this platform worker turns durable conversation/lifecycle events into
summary admissions. Delivery acknowledgments follow durable operation enqueue;
crashes between enqueue and ACK are safe through the P2-02 idempotency key.
No LLM inference, external work or conversation response is performed here.
"""

from dataclasses import dataclass
import argparse
import os
import time
import threading

import psycopg
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .event_delivery import (
    claim_delivery, defer_delivery, dispatch_outbox, process_delivery,
    resume_deferred, retry_delivery, reap_expired_claims, subscribe,
)
from .lifecycle import TrustedPlatformActor
from .operation_contracts import InvalidOperation, ResourceUnavailable, require_idle
from .runtime import StaleSelection
from .summary_contracts import StaleSummary, SummaryNotReady
from .summary_memory import schedule_summary


SUMMARY_CONSUMER = "memory.auto_summary.v1"
_RESPONSE_DELIVERED = "conversation.response.delivered.v1"
_RANGE_SUMMARIZED = "memory.conversation_range.summarized.v1"
_SELECTION_CHANGED = "persona.selection.changed.v1"
_MODE_CHANGED = "persona.mode.changed.v1"
_EVENTS = (_RESPONSE_DELIVERED, _RANGE_SUMMARIZED,
           _SELECTION_CHANGED, _MODE_CHANGED)
_TERMINAL_OR_ADMITTED = (
    "QUEUED", "RUNNING", "RETRY_WAIT", "COMPLETED", "FAILED",
    "CANCELLED", "BLOCKED", "OUTCOME_UNKNOWN", "SUSPENDED",
    "SUSPEND_REQUESTED", "CANCEL_REQUESTED",
)


@dataclass(frozen=True)
class AutoSummaryPolicy:
    """Concrete P2-03 tuning parameters, not new v2.5 domain contracts."""

    min_candidate_turns: int = 8
    retain_recent: int = 2
    max_turns: int = 8

    def __post_init__(self):
        if (not 1 <= self.min_candidate_turns <= self.max_turns <= 16
            or not 1 <= self.retain_recent <= 32):
            raise ValueError("invalid automatic summary policy")


@dataclass(frozen=True)
class AutoSummaryTick:
    dispatched: int
    processed: int
    deferred: int
    admitted: int
    replayed: int
    retry_pending: int


def register_auto_summary_consumer(
    connection: Connection, *, consumer_id: str = SUMMARY_CONSUMER,
) -> None:
    """Trusted deployment bootstrap. Register before dispatcher starts."""
    for event_type in _EVENTS:
        subscribe(connection, consumer_id, event_type)


def _current_selection(connection: Connection, user_id: UUID,
                       instance_id: UUID) -> tuple[int, str] | None:
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT generation,mode FROM persona_selection
               WHERE user_id=%s AND instance_id=%s
                 AND mode IN ('FOREGROUND','BACKGROUND')""",
            (user_id, instance_id),
        )
        row=cur.fetchone()
    return (row["generation"],row["mode"]) if row else None


def _admit_if_due(
    connection: Connection, *, user_id: UUID, instance_id: UUID,
    conversation_id: UUID, generation: int, mode: str, policy: AutoSummaryPolicy,
) -> tuple[int, int]:
    """Preflight only; P2-02 repeats all fences inside its own transactions."""
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT c.summary_watermark,c.next_sequence
               FROM conversations c
               WHERE c.conversation_id=%s AND c.user_id=%s AND c.instance_id=%s""",
            (conversation_id, user_id, instance_id),
        )
        row=cur.fetchone()
        if row is None:
            return 0,0
        eligible=min(
            policy.max_turns,
            row["next_sequence"]-1-policy.retain_recent-row["summary_watermark"],
        )
        # A meaningful departure is a natural safe point to compress even
        # short ranges. Normal foreground work waits for a useful batch.
        threshold=1 if mode=="BACKGROUND" else policy.min_candidate_turns
        if eligible<threshold:
            return 0,0
        start=row["summary_watermark"]+1
        cur.execute(
            """SELECT o.status
               FROM conversation_summary_jobs j
               JOIN operations o ON o.operation_id=j.operation_id
               WHERE j.conversation_id=%s AND j.start_sequence=%s
                 AND j.expected_generation=%s
               ORDER BY j.admission_attempt DESC LIMIT 1""",
            (conversation_id,start,generation),
        )
        last=cur.fetchone()
        if last is not None and last["status"] in _TERMINAL_OR_ADMITTED:
            # Explicit user/operator recovery owns terminal FAILED retry.
            # Active jobs are already admitted and consume no further tokens.
            return 0,0

    actor=TrustedPlatformActor(user_id=user_id,source_id=SUMMARY_CONSUMER)
    try:
        result=schedule_summary(
            connection,actor,conversation_id,instance_id,generation,
            retain_recent=policy.retain_recent,max_turns=policy.max_turns,
        )
    except (SummaryNotReady,StaleSummary):
        # Incomplete/current-generation deliveries may become eligible
        # following later delivery or persona re-selection events.
        return 0,0
    if result is None:
        return 0,0
    return (0,1) if result.replayed else (1,0)


def _claimed_event(connection: Connection, lease):
    """Read-only preflight; P0 process_delivery performs the final ACK fence."""
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT e.event_type,e.user_id,e.instance_id,e.payload
               FROM event_deliveries d
               JOIN events e ON e.event_id=d.event_id
               WHERE d.delivery_id=%s AND d.consumer_id=%s
                 AND d.status='CLAIMED' AND d.worker_id=%s
                 AND d.claim_generation=%s AND d.lease_until>clock_timestamp()""",
            (lease.delivery_id,lease.consumer_id,lease.worker_id,
             lease.claim_generation),
        )
        row=cur.fetchone()
        if row is None:
            raise StaleSelection("automatic summary delivery claim is no longer active")
        return row


def _target_conversation(connection: Connection, event: dict) -> UUID | None:
    payload=event["payload"] or {}
    user_id,instance_id=event["user_id"],event["instance_id"]
    if event["event_type"]==_RESPONSE_DELIVERED:
        source=payload.get("response_id")
        table,column="conversation_responses","response_id"
    elif event["event_type"]==_RANGE_SUMMARIZED:
        source=payload.get("summary_id")
        table,column="conversation_summaries","summary_id"
    else:
        return None
    try:
        ref=UUID(str(source))
    except (TypeError,ValueError,AttributeError):
        raise ValueError("trusted summary event missing a valid source reference")
    with connection.cursor(row_factory=dict_row) as cur:
        # Identifier names are selected from fixed internal literals above.
        cur.execute(
            f"""SELECT conversation_id FROM {table}
                WHERE {column}=%s AND user_id=%s AND instance_id=%s""",
            (ref,user_id,instance_id),
        )
        row=cur.fetchone()
    if row is None:
        raise ValueError("trusted event references no scoped conversation")
    return row["conversation_id"]


def _admit_persona_backlog(
    connection: Connection, *, user_id: UUID, instance_id: UUID,
    generation: int, mode: str, policy: AutoSummaryPolicy, limit: int,
) -> tuple[int,int]:
    """Bounded response to mode changes and selection reactivation."""
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT conversation_id FROM conversations
               WHERE user_id=%s AND instance_id=%s
               ORDER BY updated_at,conversation_id LIMIT %s""",
            (user_id,instance_id,limit),
        )
        conversations=cur.fetchall()
    admitted=replayed=0
    for row in conversations:
        new,old=_admit_if_due(
            connection,user_id=user_id,instance_id=instance_id,
            conversation_id=row["conversation_id"],generation=generation,
            mode=mode,policy=policy,
        )
        admitted+=new
        replayed+=old
    return admitted,replayed


def _resume_active(connection: Connection, consumer_id: str, limit: int) -> None:
    # Defer instead of acknowledging conversations while their persona is
    # inactive. Resume on any future worker tick after reactivation.
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT d.delivery_id FROM event_deliveries d
               JOIN events e ON e.event_id=d.event_id
               JOIN persona_selection p ON p.user_id=e.user_id
               WHERE d.consumer_id=%s AND d.status='DEFERRED'
                 AND p.instance_id=e.instance_id
                 AND p.mode IN ('FOREGROUND','BACKGROUND')
               ORDER BY e.recorded_at,d.delivery_id LIMIT %s""",
            (consumer_id,limit),
        )
        pending=[row["delivery_id"] for row in cur.fetchall()]
    for delivery_id in pending:
        resume_deferred(connection,consumer_id=consumer_id,delivery_id=delivery_id)


def _reconcile_active(connection: Connection, policy: AutoSummaryPolicy,
                      limit: int, user_id: UUID | None = None) -> tuple[int,int]:
    """Repair missed dispatcher/subscription windows and switch-back backlog.

    Skip already-admitted and not-yet-deliverable ranges *before* applying
    LIMIT. Otherwise a few blocked conversations can monopolize the scan
    page and starve unrelated eligible conversations indefinitely. Retain
    staged jobs lacking an operation row for crash recovery.
    """
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT c.conversation_id,c.user_id,c.instance_id,
                      p.generation,p.mode
               FROM conversations c
               JOIN persona_selection p ON p.user_id=c.user_id
                   AND p.instance_id=c.instance_id
               WHERE p.mode IN ('FOREGROUND','BACKGROUND')
                 AND (%s::uuid IS NULL OR c.user_id=%s::uuid)
                 AND c.next_sequence-1-c.summary_watermark-%s
                     >= CASE WHEN p.mode='BACKGROUND' THEN 1 ELSE %s END
                 AND NOT EXISTS (
                     SELECT 1 FROM conversation_summary_jobs j
                     JOIN operations o ON o.operation_id=j.operation_id
                     WHERE j.conversation_id=c.conversation_id
                       AND j.start_sequence=c.summary_watermark+1
                       AND j.expected_generation=p.generation
                       AND o.status=ANY(%s::text[])
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM conversation_turns t
                     LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
                     WHERE t.conversation_id=c.conversation_id
                       AND t.sequence BETWEEN c.summary_watermark+1
                           AND LEAST(c.summary_watermark+%s,
                                     c.next_sequence-1-%s)
                       AND (
                           t.status='PENDING'
                           OR (r.delivery_status='GENERATED'
                               AND r.selection_generation=p.generation)
                       )
                 )
               ORDER BY c.updated_at,c.conversation_id LIMIT %s""",
            (user_id,user_id,policy.retain_recent,policy.min_candidate_turns,
             list(_TERMINAL_OR_ADMITTED),policy.max_turns,policy.retain_recent,limit),
        )
        rows=cur.fetchall()
    admitted=replayed=0
    for row in rows:
        try:
            new,old=_admit_if_due(
                connection,user_id=row["user_id"],instance_id=row["instance_id"],
                conversation_id=row["conversation_id"],generation=row["generation"],
                mode=row["mode"],policy=policy,
            )
            admitted+=new
            replayed+=old
        except StaleSelection:
            continue  # selected generation changed during reconciliation
    return admitted,replayed


def run_auto_summary_tick(
    connection: Connection, *, worker_id: str,
    consumer_id: str = SUMMARY_CONSUMER,
    policy: AutoSummaryPolicy = AutoSummaryPolicy(),
    max_deliveries: int = 64, scan_limit: int = 32,
    dispatch_batches: int = 64, reconcile_user_id: UUID | None = None,
) -> AutoSummaryTick:
    """One bounded non-interactive worker cycle, with no model inference.

    Existing P0/P2 APIs perform authorization and durable admission. The
    event is acknowledged only AFTER that admission commits. Recovering a
    lost ACK is idempotent. A failed operation is never retried automatically.
    """
    require_idle(connection)
    if not 1<=max_deliveries<=1000 or not 1<=scan_limit<=1000:
        raise ValueError("invalid tick bounds")
    if not 1<=dispatch_batches<=100:
        raise ValueError("invalid dispatch batch bound")
    dispatched=processed=deferred=admitted=replayed=retry_pending=0
    for _ in range(dispatch_batches):
        n=dispatch_outbox(connection,limit=100)
        dispatched+=n
        if n==0:
            break
    reap_expired_claims(connection,limit=100)
    _resume_active(connection,consumer_id,max_deliveries)
    for _ in range(max_deliveries):
        lease=claim_delivery(
            connection,consumer_id=consumer_id,worker_id=worker_id,
            lease_seconds=120,
        )
        if lease is None:
            break
        event=_claimed_event(connection,lease)
        kind=event["event_type"]
        try:
            if kind in _EVENTS:
                selection=_current_selection(
                    connection,event["user_id"],event["instance_id"],
                )
                if selection is None:
                    if kind==_SELECTION_CHANGED:
                        # This reactivation event has itself become obsolete;
                        # current-persona reconciliation still runs every tick.
                        process_delivery(connection,lease,lambda cursor,envelope: None)
                        processed+=1
                    else:
                        defer_delivery(connection,lease)
                        deferred+=1
                    continue
                generation,mode=selection
                conversation_id=_target_conversation(connection,event)
                if conversation_id is not None:
                    new,old=_admit_if_due(
                        connection,user_id=event["user_id"],
                        instance_id=event["instance_id"],
                        conversation_id=conversation_id,generation=generation,
                        mode=mode,policy=policy,
                    )
                    admitted+=new
                    replayed+=old
                elif (kind==_SELECTION_CHANGED
                      or (kind==_MODE_CHANGED and mode=="BACKGROUND")):
                    # Reactivation and departure both deserve a bounded
                    # reconciliation before acknowledging their event.
                    new,old=_admit_persona_backlog(
                        connection,user_id=event["user_id"],
                        instance_id=event["instance_id"],generation=generation,
                        mode=mode,policy=policy,limit=scan_limit,
                    )
                    admitted+=new
                    replayed+=old
            process_delivery(connection,lease,lambda cursor,envelope: None)
            processed+=1
        except StaleSelection:
            defer_delivery(connection,lease)
            deferred+=1
        except (InvalidOperation,ResourceUnavailable) as exc:
            retry_delivery(connection,lease,error=str(exc),delay_seconds=5)
            retry_pending+=1
            break
    # A lifecycle switch does not necessarily produce a new conversation
    # event, so periodic reconciliation is required to unlock stale-gen
    # GENERATED replies and to admit jobs after operator subscription setup.
    new,old=_reconcile_active(
        connection,policy,scan_limit,user_id=reconcile_user_id,
    )
    admitted+=new
    replayed+=old
    return AutoSummaryTick(
        dispatched=dispatched,processed=processed,deferred=deferred,
        admitted=admitted,replayed=replayed,retry_pending=retry_pending,
    )


def run_auto_summary_worker(
    dsn: str, *, poll_seconds: float = 1.0,
    stop: threading.Event | None = None, worker_id: str = "auto-summary",
    policy: AutoSummaryPolicy = AutoSummaryPolicy(),
) -> None:
    """Operator-owned polling worker. Summary inference runs in another worker."""
    if not 0.1<=poll_seconds<=30:
        raise ValueError("poll_seconds must be 0.1..30")
    stopping=stop or threading.Event()
    with psycopg.connect(dsn,autocommit=True) as connection:
        register_auto_summary_consumer(connection)
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as connection:
            result=run_auto_summary_tick(
                connection,worker_id=worker_id,policy=policy,
            )
        # Keep responsiveness independent of event rate; waking on a
        # database notification may be added later without losing polling.
        if not result.dispatched and not result.processed and not result.admitted:
            stopping.wait(poll_seconds)


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(
        description="P2-03 trusted summary scheduling worker (no LLM inference)",
    )
    parser.add_argument("--dsn",default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--worker-id",default="auto-summary")
    parser.add_argument("--min-turns",type=int,default=8)
    parser.add_argument("--retain-recent",type=int,default=2)
    parser.add_argument("--max-turns",type=int,default=8)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("database URL required via --dsn or DATABASE_URL")
    policy=AutoSummaryPolicy(
        min_candidate_turns=args.min_turns,
        retain_recent=args.retain_recent,
        max_turns=args.max_turns,
    )
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as connection:
            register_auto_summary_consumer(connection)
            run_auto_summary_tick(connection,worker_id=args.worker_id,policy=policy)
    else:
        run_auto_summary_worker(
            args.dsn,poll_seconds=args.poll_seconds,
            worker_id=args.worker_id,policy=policy,
        )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
