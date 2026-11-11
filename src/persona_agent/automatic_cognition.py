"""P2-06 P0 event-driven cognitive scheduling and bounded reconciliation.

The admission worker never infers; it stages durable P1 cognitive operations
using existing scheduler authority, and acknowledges events only after admission.
The independent operation runner performs model inference outside DB locks.
"""

import argparse
import os
import threading
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .cognitive_contracts import (
    COGNITIVE_CONSUMER,CognitiveNotReady,InvalidCognitiveProposal,
    StaleCognition,
)
from .cognitive_engine import run_one_cognitive_operation,schedule_cognition
from .event_delivery import (
    StaleDelivery,claim_delivery,defer_delivery,dispatch_outbox,
    process_delivery,reap_expired_claims,resume_deferred,retry_delivery,subscribe,
)
from .lifecycle import TrustedPlatformActor
from .model_gateway import OllamaChatGateway
from .conversation_contracts import ModelUnavailable
from .operation_contracts import StaleOperation
from .operation_contracts import InvalidOperation,ResourceUnavailable,require_idle
from .runtime import StaleSelection

_EVENTS=(
    "conversation.response.delivered.v1",
    "conversation.turn.superseded.v1",
    "persona.selection.changed.v1",
    "persona.mode.changed.v1",
)


@dataclass(frozen=True)
class CognitivePolicy:
    min_closed_turns:int=3
    retain_recent:int=1
    max_turns:int=8

    def __post_init__(self):
        if not 1<=self.min_closed_turns<=self.max_turns<=16:
            raise ValueError("invalid cognitive minimum")
        if not 1<=self.retain_recent<=16:
            raise ValueError("invalid cognitive recent retention")


@dataclass(frozen=True)
class CognitiveTick:
    dispatched:int
    processed:int
    deferred:int
    admitted:int
    replayed:int
    retry_pending:int


def register_cognitive_consumer(
    connection:Connection,consumer_id:str=COGNITIVE_CONSUMER,
) -> None:
    for kind in _EVENTS:
        subscribe(connection,consumer_id,kind)


def _selection(connection:Connection,user_id:UUID,instance_id:UUID):
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT generation,mode FROM persona_selection
               WHERE user_id=%s AND instance_id=%s
                 AND mode IN ('FOREGROUND','BACKGROUND')""",
            (user_id,instance_id),
        )
        return cur.fetchone()


def _event_claim(connection:Connection,lease):
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
        current=cur.fetchone()
        if current is None:
            raise StaleDelivery("cognitive event claim expired")
        return current


def _event_conversation_id(connection:Connection,event:dict) -> UUID|None:
    """Resolve trusted conversation event payloads without assuming its shape.

    Delivery and supersession events contain turn_id, while input events
    carry conversation_id. Re-check the scoped turn ownership in SQL.
    """
    if not event["event_type"].startswith("conversation."):
        return None
    payload=event["payload"]
    if not isinstance(payload,dict):
        return None
    raw=payload.get("conversation_id")
    if raw is not None:
        try:
            return UUID(str(raw))
        except (ValueError,TypeError):
            return None
    raw=payload.get("turn_id")
    if raw is None:
        return None
    try:
        turn_id=UUID(str(raw))
    except (ValueError,TypeError):
        return None
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT conversation_id FROM conversation_turns
               WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
            (turn_id,event["user_id"],event["instance_id"]),
        )
        row=cur.fetchone()
        return row["conversation_id"] if row else None


def _admit(connection,user_id:UUID,instance_id:UUID,
           conversation_id:UUID,policy:CognitivePolicy) -> tuple[int,int]:
    selected=_selection(connection,user_id,instance_id)
    if selected is None:
        raise StaleSelection("cognitive event belongs to inactive persona")
    minimum=1 if selected["mode"]=="BACKGROUND" else policy.min_closed_turns
    outcome=schedule_cognition(
        connection,TrustedPlatformActor(
            user_id=user_id,source_id=COGNITIVE_CONSUMER,
        ),conversation_id,instance_id,selected["generation"],
        retain_recent=policy.retain_recent,max_turns=policy.max_turns,
        min_closed_turns=minimum,
    )
    return (
        (int(not outcome.replayed),int(outcome.replayed)) if outcome is not None
        else (0,0)
    )


def _eligible_rows(
    connection:Connection,policy:CognitivePolicy,scan_limit:int,
    user_id:UUID|None,
) -> list[dict]:
    """Read only ready, not-yet-admitted candidates before applying LIMIT."""
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT c.conversation_id,c.user_id,c.instance_id
               FROM conversations c
               JOIN persona_selection p ON p.user_id=c.user_id
                 AND p.instance_id=c.instance_id
               LEFT JOIN LATERAL (
                   SELECT j.operation_id,j.end_sequence,
                          o.operation_id AS enqueued_operation_id,
                          o.status AS operation_status
                   FROM cognitive_jobs j LEFT JOIN operations o
                     ON o.operation_id=j.operation_id
                   WHERE j.conversation_id=c.conversation_id
                     AND j.start_sequence=c.cognitive_watermark+1
                     AND j.expected_generation=p.generation
                   ORDER BY j.admission_attempt DESC LIMIT 1
               ) staged ON TRUE
               WHERE p.mode IN ('FOREGROUND','BACKGROUND')
                 AND (%s::uuid IS NULL OR c.user_id=%s::uuid)
                 AND (
                   c.next_sequence-1-c.cognitive_watermark-%s
                     >= CASE WHEN p.mode='BACKGROUND' THEN 1 ELSE %s END
                   OR (staged.operation_id IS NOT NULL
                       AND staged.enqueued_operation_id IS NULL)
                 )
                 AND staged.operation_status IS NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM conversation_turns t
                    LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
                    WHERE t.conversation_id=c.conversation_id
                      AND t.sequence BETWEEN c.cognitive_watermark+1
                        AND CASE
                          WHEN staged.operation_id IS NOT NULL
                            AND staged.enqueued_operation_id IS NULL
                          THEN staged.end_sequence
                          ELSE LEAST(c.cognitive_watermark+%s,
                                    c.next_sequence-1-%s)
                        END
                      AND (t.status='PENDING' OR (
                          r.delivery_status='GENERATED'
                          AND r.selection_generation=p.generation
                      ))
                 )
               ORDER BY c.updated_at,c.conversation_id LIMIT %s""",
            (user_id,user_id,policy.retain_recent,policy.min_closed_turns,
             policy.max_turns,policy.retain_recent,scan_limit),
        )
        return [dict(row) for row in cur.fetchall()]


def _reconcile(
    connection:Connection,policy:CognitivePolicy,scan_limit:int,
    user_id:UUID|None,
) -> tuple[int,int]:
    admitted=replayed=0
    for candidate in _eligible_rows(connection,policy,scan_limit,user_id):
        try:
            new,again=_admit(
                connection,candidate["user_id"],candidate["instance_id"],
                candidate["conversation_id"],policy,
            )
            admitted+=new
            replayed+=again
        except (StaleSelection,CognitiveNotReady,ResourceUnavailable,InvalidOperation):
            continue
    return admitted,replayed


def _resume_selected(connection:Connection,consumer_id:str,limit:int):
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
        ready=[x["delivery_id"] for x in cur.fetchall()]
    for delivery_id in ready:
        resume_deferred(
            connection,consumer_id=consumer_id,delivery_id=delivery_id,
        )


def run_cognitive_scheduling_tick(
    connection:Connection,*,worker_id:str,
    consumer_id:str=COGNITIVE_CONSUMER,
    policy:CognitivePolicy=CognitivePolicy(),
    max_deliveries:int=64,scan_limit:int=32,
    dispatch_batches:int=32,reconcile_user_id:UUID|None=None,
) -> CognitiveTick:
    """Bounded SQL-only tick; all P0 event leases are fenced."""
    require_idle(connection)
    if not 1<=max_deliveries<=1000 or not 1<=scan_limit<=1000:
        raise ValueError("invalid cognitive scheduling bounds")
    if not 1<=dispatch_batches<=100:
        raise ValueError("invalid cognitive outbox bound")
    dispatched=processed=deferred=admitted=replayed=retry_pending=0
    for _ in range(dispatch_batches):
        n=dispatch_outbox(connection,limit=100)
        dispatched+=n
        if n==0:
            break
    reap_expired_claims(connection,limit=100)
    _resume_selected(connection,consumer_id,max_deliveries)
    for _ in range(max_deliveries):
        lease=claim_delivery(
            connection,consumer_id=consumer_id,worker_id=worker_id,
            lease_seconds=120,
        )
        if lease is None:
            break
        try:
            event=_event_claim(connection,lease)
            selected=_selection(connection,event["user_id"],event["instance_id"])
            if selected is None:
                if event["event_type"]=="persona.selection.changed.v1":
                    # Obsolete selection events are not authorization to
                    # reactivate a different persona.
                    process_delivery(connection,lease,lambda cur,envelope: None)
                    processed+=1
                else:
                    defer_delivery(connection,lease)
                    deferred+=1
                continue
            conversation_id=_event_conversation_id(connection,event)
            if conversation_id is not None:
                new,again=_admit(
                    connection,event["user_id"],event["instance_id"],
                    conversation_id,policy,
                )
                admitted+=new
                replayed+=again
            process_delivery(connection,lease,lambda cur,envelope: None)
            processed+=1
        except StaleDelivery:
            continue
        except StaleSelection:
            try:
                defer_delivery(connection,lease)
                deferred+=1
            except StaleDelivery:
                pass
        except (CognitiveNotReady,):
            # The conversation may still have an undelivered response.
            # Reconciliation repairs it after a later delivery/state change.
            try:
                process_delivery(connection,lease,lambda cur,envelope:None)
                processed+=1
            except StaleDelivery:
                pass
        except (ResourceUnavailable,InvalidOperation) as exc:
            try:
                retry_delivery(connection,lease,error=str(exc),delay_seconds=5)
                retry_pending+=1
            except StaleDelivery:
                pass
            break
    new,again=_reconcile(
        connection,policy,scan_limit,reconcile_user_id,
    )
    admitted+=new
    replayed+=again
    return CognitiveTick(
        dispatched,processed,deferred,admitted,replayed,retry_pending,
    )


def run_cognitive_worker(
    dsn:str,*,model:str,worker_id:str="cognitive-worker",
    poll_seconds:float=1.0,stop:threading.Event|None=None,
) -> None:
    """Operator-owned service. At most one bounded P0 operation per tick."""
    if not 0.1<=poll_seconds<=30:
        raise ValueError("poll_seconds must be 0.1..30")
    gateway=OllamaChatGateway(model=model)
    stopping=stop or threading.Event()
    with psycopg.connect(dsn,autocommit=True) as connection:
        register_cognitive_consumer(connection)
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as connection:
            scheduled=run_cognitive_scheduling_tick(
                connection,worker_id=worker_id,
            )
            try:
                completed=run_one_cognitive_operation(
                    connection,gateway,worker_id=worker_id,
                )
            except (InvalidCognitiveProposal,ModelUnavailable,
                    StaleOperation,StaleCognition,CognitiveNotReady):
                # P0 retry/fencing already owns the failed or revoked claim.
                # One bad model pass must not stop scheduling for all users.
                completed=None
        if (completed is None and scheduled.dispatched==0
            and scheduled.processed==0 and scheduled.admitted==0):
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="P2-06 asynchronous cognitive worker")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--model",default=os.getenv("COGNITIVE_MODEL"))
    parser.add_argument("--worker-id",default="cognitive-worker")
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--once",action="store_true",help="schedule only, no model inference")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn or DATABASE_URL required")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as connection:
            register_cognitive_consumer(connection)
            run_cognitive_scheduling_tick(connection,worker_id=args.worker_id)
        return 0
    if not args.model:
        parser.error("--model or COGNITIVE_MODEL required for the inference worker")
    run_cognitive_worker(
        args.dsn,model=args.model,worker_id=args.worker_id,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
