"""P2-07 P0 delivery consumer for independent emotion and relationship domains.

Each owner commits in its own event-delivery transaction. No background
scheduler, model call, global state or inferred cross-domain write is added.
"""

import argparse
import os
import threading
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .emotion_state import EMOTION_CONSUMER,observe_emotion_input
from .relationship_state import RELATIONSHIP_CONSUMER,observe_relationship_input
from .event_delivery import (
    StaleDelivery,claim_delivery,defer_delivery,dispatch_outbox,
    process_delivery,reap_expired_claims,resume_deferred,retry_delivery,
    subscribe,
)
from .operation_contracts import require_idle
from .runtime import PermissionDenied

_INPUT_EVENT="conversation.user_text.received.v1"


class InactiveAffect(Exception):
    """Consumer must defer rather than appraise an unselected persona."""


@dataclass(frozen=True)
class AffectTick:
    dispatched:int
    processed:int
    deferred:int
    emotion_applied:int
    relationship_recorded:int
    retry_pending:int


def register_affect_consumers(connection:Connection)->None:
    """Register both P0 subscriptions before initial outbox dispatch."""
    subscribe(connection,EMOTION_CONSUMER,_INPUT_EVENT)
    subscribe(connection,RELATIONSHIP_CONSUMER,_INPUT_EVENT)


def _source(cur,event):
    if (event.event_type!=_INPUT_EVENT
        or event.trust_level!="AUTHENTICATED_USER"
        or not isinstance(event.payload,dict)):
        raise PermissionDenied("affect updates require authenticated user input")
    try:
        turn_id=UUID(str(event.payload["turn_id"]))
        conversation_id=UUID(str(event.payload["conversation_id"]))
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise ValueError("input event missing source turn identifiers") from exc
    cur.execute(
        """SELECT t.turn_id,t.conversation_id,t.input_text,t.created_at,t.source_id,
                  t.selection_generation
           FROM conversation_turns t
           WHERE t.turn_id=%s AND t.conversation_id=%s
             AND t.user_id=%s AND t.instance_id=%s""",
        (turn_id,conversation_id,event.user_id,event.instance_id),
    )
    source=cur.fetchone()
    if source is None:
        raise PermissionDenied("source turn outside authenticated event scope")
    # Inactive personas receive no new simulated-mood/relationship appraisals.
    # Deferred event delivery survives restart and resumes on reselection.
    cur.execute(
        """SELECT instance_id,mode FROM persona_selection
           WHERE user_id=%s FOR SHARE""",
        (event.user_id,),
    )
    selected=cur.fetchone()
    if (selected is None or selected["instance_id"]!=event.instance_id
        or selected["mode"] not in ("FOREGROUND","BACKGROUND")):
        raise InactiveAffect("persona inactive until explicitly reselected")
    return dict(source)


def _handler(cur,event,consumer:str,outcomes:list[str])->None:
    source=_source(cur,event)
    if consumer==EMOTION_CONSUMER:
        if observe_emotion_input(cur,event,source):
            outcomes.append("EMOTION")
    elif consumer==RELATIONSHIP_CONSUMER:
        if observe_relationship_input(cur,event,source):
            outcomes.append("RELATIONSHIP")
    else:
        raise ValueError("unknown affect event consumer")


def _resume_selected(connection:Connection,consumer:str,limit:int)->None:
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT d.delivery_id FROM event_deliveries d
               JOIN events e ON e.event_id=d.event_id
               JOIN persona_selection p ON p.user_id=e.user_id
               WHERE d.consumer_id=%s AND d.status='DEFERRED'
                 AND p.instance_id=e.instance_id
                 AND p.mode IN ('FOREGROUND','BACKGROUND')
               ORDER BY e.recorded_at,d.delivery_id LIMIT %s""",
            (consumer,limit),
        )
        ids=[row["delivery_id"] for row in cur.fetchall()]
    for delivery_id in ids:
        resume_deferred(
            connection,consumer_id=consumer,delivery_id=delivery_id,
        )


def run_affect_tick(
    connection:Connection,*,worker_id:str,
    max_deliveries_per_domain:int=64,dispatch_batches:int=16,
) -> AffectTick:
    """Bounded SQL-only P0 cycle. ACK and domain effects are atomic."""
    require_idle(connection)
    if not 1<=max_deliveries_per_domain<=500 or not 1<=dispatch_batches<=100:
        raise ValueError("invalid affect worker bounds")
    dispatched=processed=deferred=emotion=relationship=retries=0
    for _ in range(dispatch_batches):
        n=dispatch_outbox(connection,limit=100)
        dispatched+=n
        if n==0:
            break
    reap_expired_claims(connection,limit=100)
    for consumer in (EMOTION_CONSUMER,RELATIONSHIP_CONSUMER):
        _resume_selected(connection,consumer,max_deliveries_per_domain)
        for _ in range(max_deliveries_per_domain):
            lease=claim_delivery(
                connection,consumer_id=consumer,worker_id=worker_id,
                lease_seconds=120,
            )
            if lease is None:
                break
            decisions=[]
            try:
                process_delivery(
                    connection,lease,
                    lambda cur,event: _handler(cur,event,consumer,decisions),
                )
                processed+=1
                emotion+=decisions.count("EMOTION")
                relationship+=decisions.count("RELATIONSHIP")
            except InactiveAffect:
                try:
                    defer_delivery(connection,lease)
                    deferred+=1
                except StaleDelivery:
                    pass
            except StaleDelivery:
                continue
            except (PermissionDenied,ValueError) as exc:
                try:
                    retry_delivery(connection,lease,error=str(exc),delay_seconds=5)
                    retries+=1
                except StaleDelivery:
                    pass
    return AffectTick(
        dispatched,processed,deferred,emotion,relationship,retries,
    )


def run_affect_worker(
    dsn:str,*,worker_id:str="affect-worker",poll_seconds:float=1.0,
    stop:threading.Event|None=None,
)->None:
    """Independent of foreground generation, inference, and P1 GPU slots."""
    if not 0.1<=poll_seconds<=30:
        raise ValueError("poll_seconds must be 0.1..30")
    stopping=stop or threading.Event()
    with psycopg.connect(dsn,autocommit=True) as conn:
        register_affect_consumers(conn)
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as conn:
            result=run_affect_tick(conn,worker_id=worker_id)
        if result.dispatched==0 and result.processed==0:
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="P2-07 affect and relationship consumer")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--worker-id",default="affect-worker")
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("DATABASE_URL or --dsn required")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as conn:
            register_affect_consumers(conn)
            run_affect_tick(conn,worker_id=args.worker_id)
        return 0
    run_affect_worker(
        args.dsn,worker_id=args.worker_id,poll_seconds=args.poll_seconds,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
