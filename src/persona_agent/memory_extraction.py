"""P2-04 explicit memory extraction, audited consolidation and review.

This is a bounded, deterministic first slice: it observes authenticated user
input events in P0's database-only delivery handler. No LLM interpretation is
promoted to memory. Ambiguous and sensitive statements are not extracted.
Contradictory explicit claims are kept as candidates, never silently amended.
"""

import argparse
import os
import re
import threading
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .memory_explicit_grammar import (
    ExplicitClaim, parse_explicit_claim, _slug, _supported_safe_claim,
)
from .conversation import _require_actor
from .event_delivery import (
    StaleDelivery, claim_delivery, defer_delivery, dispatch_outbox,
    process_delivery, reap_expired_claims, resume_deferred, retry_delivery,
    subscribe,
)
from .lifecycle import TrustedPlatformActor
from .memory import _amend_memory_in_transaction, _event, _evidence, _revision
from .memory_contracts import AdmitMemoryCommand, AmendMemoryCommand, MemoryWriteResult
from .operation_contracts import require_idle
from .runtime import PermissionDenied, VersionConflict


EXTRACTION_CONSUMER = "memory.explicit_extraction.v1"
_INPUT_EVENT = "conversation.user_text.received.v1"

@dataclass(frozen=True)
class ExtractionTick:
    dispatched: int
    processed: int
    deferred: int
    admitted: int
    corroborated: int
    conflicts: int
    stale: int
    retry_pending: int


@dataclass(frozen=True)
class ConflictCandidate:
    candidate_id: UUID
    memory_id: UUID
    expected_version: int
    memory_kind: str
    subject_key: str
    predicate_key: str
    proposed_value: str
    current_value: str
    source_turn_id: UUID
    conversation_id: UUID
    evidence_quote: str


class InactiveExtraction(Exception):
    """Persona is not selected, so the delivery must remain recoverable."""


def register_extraction_consumer(
    connection: Connection, *, consumer_id: str = EXTRACTION_CONSUMER,
) -> None:
    """Trusted bootstrap before dispatch. Existing P0 backfill is available."""
    subscribe(connection,consumer_id,_INPUT_EVENT)


def _source(cur, event):
    if (event.event_type!=_INPUT_EVENT
        or event.trust_level!="AUTHENTICATED_USER"
        or not isinstance(event.payload,dict)):
        raise PermissionDenied("memory extraction requires an authenticated input event")
    try:
        turn_id=UUID(event.payload["turn_id"])
        conversation_id=UUID(event.payload["conversation_id"])
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise ValueError("input event missing valid turn provenance") from exc
    cur.execute(
        """SELECT t.input_text,t.created_at,t.source_id,t.selection_generation
           FROM conversation_turns t WHERE t.turn_id=%s AND t.conversation_id=%s
             AND t.user_id=%s AND t.instance_id=%s""",
        (turn_id,conversation_id,event.user_id,event.instance_id),
    )
    source=cur.fetchone()
    if source is None:
        raise PermissionDenied("input turn outside authenticated event scope")
    cur.execute(
        """SELECT instance_id,mode FROM persona_selection
           WHERE user_id=%s FOR SHARE""",(event.user_id,),
    )
    selected=cur.fetchone()
    if (selected is None or selected["instance_id"]!=event.instance_id
        or selected["mode"] not in ("FOREGROUND","BACKGROUND")):
        raise InactiveExtraction("input belongs to an inactive persona")
    return conversation_id,turn_id,source


def _persist_claim(cur,event,conversation_id,turn_id,source,claim: ExplicitClaim):
    # Defense in depth: the Memory write boundary must enforce the positive
    # safe vocabulary even when a candidate bypasses the public parser.
    if (claim.subject_key!="user"
        or not _supported_safe_claim(
            claim.memory_kind,claim.predicate_key,claim.value_text
        )):
        return None
    candidate_id=uuid5(
        NAMESPACE_URL,
        f"persona-agent/memory-extract/{turn_id}/{claim.memory_kind}/"
        f"{claim.subject_key}/{claim.predicate_key}",
    )
    cur.execute(
        """INSERT INTO memory_extraction_candidates(
             candidate_id,user_id,instance_id,conversation_id,source_turn_id,
             source_event_id,memory_kind,subject_key,predicate_key,value_text,
             source_quote,confidence,status
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')
           ON CONFLICT(source_turn_id,memory_kind,subject_key,predicate_key)
           DO NOTHING RETURNING candidate_id""",
        (candidate_id,event.user_id,event.instance_id,conversation_id,turn_id,
         event.event_id,claim.memory_kind,claim.subject_key,claim.predicate_key,
         claim.value_text,claim.source_quote,claim.confidence),
    )
    if cur.fetchone() is None:
        # An earlier subscriber/worker has already committed this source.
        return None

    actor=TrustedPlatformActor(user_id=event.user_id,source_id=EXTRACTION_CONSUMER)
    # Validated even though the candidate grammar uses a direct substring.
    evidence=_evidence(cur,actor,event.instance_id,conversation_id,
                       turn_id,claim.source_quote)
    record_id=uuid4()
    cur.execute(
        """INSERT INTO memory_records(
             memory_id,user_id,instance_id,memory_kind,subject_key,predicate_key,
             value_text,confidence,privacy_class,source_conversation_id,
             source_turn_id,source_turn_at,source_quote,source_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'NORMAL',%s,%s,%s,%s,%s)
           ON CONFLICT(user_id,instance_id,memory_kind,subject_key,predicate_key)
           DO NOTHING RETURNING memory_id""",
        (record_id,event.user_id,event.instance_id,claim.memory_kind,
         claim.subject_key,claim.predicate_key,claim.value_text,claim.confidence,
         conversation_id,turn_id,source["created_at"],
         claim.source_quote,source["source_id"]),
    )
    inserted=cur.fetchone() is not None
    if inserted:
        version=1
        status="ADMITTED"
        command=AdmitMemoryCommand(
            instance_id=event.instance_id,
            expected_generation=source["selection_generation"],
            conversation_id=conversation_id,evidence_turn_id=turn_id,
            evidence_quote=claim.source_quote,memory_kind=claim.memory_kind,
            subject_key=claim.subject_key,predicate_key=claim.predicate_key,
            value_text=claim.value_text,confidence=claim.confidence,
            idempotency_key=f"extract:{candidate_id}",
        )
        event_id=_event(
            cur,actor,event.instance_id,record_id,version,
            "memory.record.admitted.v1",turn_id,event.event_id,
            trust_level="TRUSTED_PLATFORM",
        )
        _revision(
            cur,actor,command,record_id,version,"ADMIT",claim.value_text,
            "ACTIVE",claim.confidence,evidence,event_id,
        )
    else:
        cur.execute(
            """SELECT memory_id,version,value_text,record_status,source_turn_at
               FROM memory_records
               WHERE user_id=%s AND instance_id=%s AND memory_kind=%s
                 AND subject_key=%s AND predicate_key=%s FOR UPDATE""",
            (event.user_id,event.instance_id,claim.memory_kind,
             claim.subject_key,claim.predicate_key),
        )
        current=cur.fetchone()
        if current is None:
            raise RuntimeError("memory slot disappeared after uniqueness conflict")
        record_id=current["memory_id"]
        version=current["version"]
        if source["created_at"]<current["source_turn_at"]:
            status="STALE"
        elif (current["record_status"]=="ACTIVE"
              and current["value_text"]==claim.value_text):
            status="CORROBORATED"
        else:
            status="CONFLICT"

    if status in ("ADMITTED","CORROBORATED"):
        cur.execute(
            """INSERT INTO memory_claim_support(
                 memory_id,version,conversation_id,source_turn_id,user_id,
                 instance_id,source_quote
               ) VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(memory_id,version,source_turn_id) DO NOTHING""",
            (record_id,version,conversation_id,turn_id,event.user_id,
             event.instance_id,claim.source_quote),
        )

    cur.execute(
        """UPDATE memory_extraction_candidates
           SET status=%s,target_memory_id=%s,target_version=%s
           WHERE candidate_id=%s""",
        (status,record_id,version,candidate_id),
    )
    if status=="CONFLICT":
        _event(
            cur,actor,event.instance_id,record_id,version,
            "memory.candidate.conflict.v1",turn_id,event.event_id,
            trust_level="TRUSTED_PLATFORM",
        )
    return status


def _handle_input(cur,event,decisions: list[str]):
    conversation_id,turn_id,source=_source(cur,event)
    claim=parse_explicit_claim(source["input_text"])
    if claim is None:
        return
    status=_persist_claim(cur,event,conversation_id,turn_id,source,claim)
    if status is not None:
        decisions.append(status)


def _resume_selected(connection: Connection, consumer_id: str, limit: int) -> None:
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
        ready=[row["delivery_id"] for row in cur.fetchall()]
    for delivery_id in ready:
        resume_deferred(connection,consumer_id=consumer_id,delivery_id=delivery_id)


def run_extraction_tick(
    connection: Connection, *, worker_id: str,
    consumer_id: str = EXTRACTION_CONSUMER,
    max_deliveries: int = 64, dispatch_batches: int = 32,
) -> ExtractionTick:
    """One bounded SQL-only, at-least-once consumer cycle; no model or tools."""
    require_idle(connection)
    if not 1<=max_deliveries<=1000 or not 1<=dispatch_batches<=100:
        raise ValueError("invalid extraction bounds")
    dispatched=processed=deferred=retry_pending=0
    admitted=corroborated=conflicts=stale=0
    for _ in range(dispatch_batches):
        n=dispatch_outbox(connection,limit=100)
        dispatched+=n
        if n==0:
            break
    reap_expired_claims(connection,limit=100)
    _resume_selected(connection,consumer_id,max_deliveries)
    for _ in range(max_deliveries):
        lease=claim_delivery(connection,consumer_id=consumer_id,worker_id=worker_id,
                             lease_seconds=120)
        if lease is None:
            break
        decisions=[]
        try:
            process_delivery(
                connection,lease,lambda cur,event: _handle_input(cur,event,decisions),
            )
            processed+=1
            admitted+=decisions.count("ADMITTED")
            corroborated+=decisions.count("CORROBORATED")
            conflicts+=decisions.count("CONFLICT")
            stale+=decisions.count("STALE")
        except InactiveExtraction:
            try:
                defer_delivery(connection,lease)
                deferred+=1
            except StaleDelivery:
                pass
        except StaleDelivery:
            continue
        except (ValueError,PermissionDenied) as exc:
            try:
                retry_delivery(connection,lease,error=str(exc),delay_seconds=5)
                retry_pending+=1
            except StaleDelivery:
                pass
    return ExtractionTick(
        dispatched,processed,deferred,admitted,corroborated,conflicts,
        stale,retry_pending,
    )


def _reassess_conflict(cur, candidate: dict) -> dict:
    """Re-evaluate one locked candidate against the current memory revision.

    This may retire historical evidence, corroborate the current value, or
    retarget a newer conflicting claim for explicit user approval. It never
    applies a memory correction or changes the source evidence.
    """
    if candidate["status"]!="CONFLICT":
        return candidate
    cur.execute(
        """SELECT m.version,m.value_text,m.record_status,m.source_turn_at,
                  t.created_at AS candidate_turn_at
           FROM memory_records m
           JOIN conversation_turns t ON t.turn_id=%s
             AND t.conversation_id=%s AND t.user_id=m.user_id
             AND t.instance_id=m.instance_id
           WHERE m.memory_id=%s AND m.user_id=%s AND m.instance_id=%s""",
        (candidate["source_turn_id"],candidate["conversation_id"],
         candidate["target_memory_id"],candidate["user_id"],
         candidate["instance_id"]),
    )
    current=cur.fetchone()
    if current is None:
        raise PermissionDenied("conflict source or memory outside candidate scope")
    if candidate["target_version"]==current["version"]:
        return candidate
    if current["candidate_turn_at"]<=current["source_turn_at"]:
        new_status="STALE"
    elif (current["record_status"]=="ACTIVE"
          and candidate["value_text"]==current["value_text"]):
        new_status="CORROBORATED"
    else:
        new_status="CONFLICT"
    cur.execute(
        """UPDATE memory_extraction_candidates
           SET status=%s,target_version=%s,
               reviewed_at=CASE WHEN %s='STALE' THEN clock_timestamp()
                                ELSE reviewed_at END
           WHERE candidate_id=%s AND user_id=%s
             AND status='CONFLICT' AND target_version=%s
           RETURNING candidate_id""",
        (new_status,current["version"],new_status,
         candidate["candidate_id"],candidate["user_id"],
         candidate["target_version"]),
    )
    if cur.fetchone() is None:
        raise VersionConflict("candidate changed during conflict re-evaluation")
    if new_status=="CORROBORATED":
        cur.execute(
            """INSERT INTO memory_claim_support(
                 memory_id,version,conversation_id,source_turn_id,user_id,
                 instance_id,source_quote
               ) VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(memory_id,version,source_turn_id) DO NOTHING""",
            (candidate["target_memory_id"],current["version"],
             candidate["conversation_id"],candidate["source_turn_id"],
             candidate["user_id"],candidate["instance_id"],
             candidate["source_quote"]),
        )
    return {**candidate,"status":new_status,
            "target_version":current["version"]}


def _refresh_review_queue(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    limit: int,
) -> None:
    """Bounded candidate-only reconciliation before presenting the queue.

    Lock candidates consistently, using SKIP LOCKED to avoid contention
    with concurrent approvals. Never lock authoritative memory rows here.
    """
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT c.* FROM memory_extraction_candidates c
                   JOIN memory_records m ON m.memory_id=c.target_memory_id
                     AND m.user_id=c.user_id AND m.instance_id=c.instance_id
                   WHERE c.user_id=%s AND c.instance_id=%s
                     AND c.status='CONFLICT' AND c.target_version<>m.version
                   ORDER BY c.created_at,c.candidate_id
                   LIMIT %s FOR UPDATE OF c SKIP LOCKED""",
                (actor.user_id,instance_id,limit),
            )
            candidates=cur.fetchall()
            for candidate in candidates:
                _reassess_conflict(cur,candidate)


def list_extraction_conflicts(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    *, limit: int = 20,
) -> tuple[ConflictCandidate,...]:
    """User-scoped queue with bounded candidate-decision reconciliation.

    Inactive personas can be inspected. Only candidate metadata and support
    are refreshed, never the authoritative memory without approval.
    """
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=100:
        raise ValueError("invalid review page size")
    _refresh_review_queue(connection,actor,instance_id,max(64,4*limit))
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
            (actor.user_id,instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("memory conflict review outside user scope")
        cur.execute(
            """SELECT c.candidate_id,c.target_memory_id AS memory_id,
                      c.target_version AS expected_version,c.memory_kind,
                      c.subject_key,c.predicate_key,c.value_text AS proposed_value,
                      m.value_text AS current_value,c.source_turn_id,
                      c.conversation_id,c.source_quote AS evidence_quote
               FROM memory_extraction_candidates c
               JOIN memory_records m ON m.memory_id=c.target_memory_id
                   AND m.user_id=c.user_id AND m.instance_id=c.instance_id
               JOIN conversation_turns t ON t.turn_id=c.source_turn_id
                 AND t.conversation_id=c.conversation_id
                 AND t.user_id=c.user_id AND t.instance_id=c.instance_id
               WHERE c.user_id=%s AND c.instance_id=%s AND c.status='CONFLICT'
                 AND c.target_version=m.version
                 AND t.created_at>m.source_turn_at
                 AND (m.record_status<>'ACTIVE' OR c.value_text<>m.value_text)
               ORDER BY c.created_at DESC,c.candidate_id LIMIT %s""",
            (actor.user_id,instance_id,limit),
        )
        return tuple(ConflictCandidate(**row) for row in cur.fetchall())


def approve_extraction_conflict(
    connection: Connection, actor: TrustedActor, candidate_id: UUID,
    *, expected_generation: int, expected_version: int,
) -> MemoryWriteResult:
    """Atomically approve a conflict and revise the Memory-owned record.

    The candidate row serializes concurrent approvals. Its decision and all
    P2-01 correction effects (record, revision, receipt, event and outbox)
    share exactly one short PostgreSQL transaction. A crash rolls back both.
    No model or platform actor may approve a conflict.
    """
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM memory_extraction_candidates
                   WHERE candidate_id=%s AND user_id=%s FOR UPDATE""",
                (candidate_id,actor.user_id),
            )
            candidate=cur.fetchone()
            if candidate is None:
                raise PermissionDenied("candidate outside authenticated user scope")
            candidate=_reassess_conflict(cur,candidate)
            if candidate["status"] not in ("CONFLICT","APPROVED"):
                raise ValueError("candidate is not a reviewable conflicting claim")
            if (candidate["status"]=="CONFLICT"
                and candidate["target_version"]!=expected_version):
                raise VersionConflict("candidate was evaluated against another revision")
            command=AmendMemoryCommand(
                instance_id=candidate["instance_id"],
                expected_generation=expected_generation,
                memory_id=candidate["target_memory_id"],
                expected_version=expected_version,
                conversation_id=candidate["conversation_id"],
                evidence_turn_id=candidate["source_turn_id"],
                evidence_quote=candidate["source_quote"],
                action="CORRECT",new_value=candidate["value_text"],
                confidence=float(candidate["confidence"]),
                idempotency_key=f"approve-extraction:{candidate_id}",
            )
            result=_amend_memory_in_transaction(cur,actor,command)
            if candidate["status"]=="APPROVED":
                # A correctly finalized candidate must replay its exact
                # original command receipt, not create another revision.
                if (not result.replayed
                    or candidate["target_version"]!=result.version):
                    raise VersionConflict("approved candidate receipt is inconsistent")
                return result
            cur.execute(
                """UPDATE memory_extraction_candidates
                   SET status='APPROVED',target_version=%s,
                       reviewed_at=clock_timestamp()
                   WHERE candidate_id=%s AND user_id=%s AND status='CONFLICT'
                     AND target_memory_id=%s AND target_version=%s
                   RETURNING candidate_id""",
                (result.version,candidate_id,actor.user_id,
                 candidate["target_memory_id"],expected_version),
            )
            if cur.fetchone() is None:
                raise VersionConflict("candidate changed during correction")
            return result


def run_extraction_worker(
    dsn: str, *, worker_id: str = "memory-extract",
    poll_seconds: float = 1.0, stop: threading.Event | None = None,
) -> None:
    """Operator-owned worker, separate from interactive response latency."""
    if not 0.1<=poll_seconds<=30:
        raise ValueError("poll_seconds must be 0.1..30")
    stopping=stop or threading.Event()
    with psycopg.connect(dsn,autocommit=True) as conn:
        register_extraction_consumer(conn)
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as conn:
            result=run_extraction_tick(conn,worker_id=worker_id)
        if result.processed==0 and result.dispatched==0:
            stopping.wait(poll_seconds)


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="P2-04 explicit memory extraction")
    parser.add_argument("--dsn",default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--worker-id",default="memory-extract")
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("database URL required via --dsn or DATABASE_URL")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as conn:
            register_extraction_consumer(conn)
            run_extraction_tick(conn,worker_id=args.worker_id)
    else:
        run_extraction_worker(args.dsn,worker_id=args.worker_id,
                              poll_seconds=args.poll_seconds)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
