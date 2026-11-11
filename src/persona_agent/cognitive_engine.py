"""P2-06 durable asynchronous cognition without hidden reasoning transcripts.

Cognition owns only MODEL_INTERPRETED, evidence-linked proposals. P0 owns
operation leases and resources; Conversation owns input/watermark; Memory,
Goals, Emotion and Relationships remain their own write authorities.
"""

import argparse
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from uuid import NAMESPACE_URL,UUID,uuid4,uuid5

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import ValidationError

from .cognitive_contracts import (
    COGNITIVE_CAPABILITY, COGNITIVE_OPERATION, COGNITIVE_RESOURCE,
    CognitiveCommitted, CognitiveDraft, CognitiveNotReady, CognitiveScheduled,
    CognitiveWork, InvalidCognitiveProposal, StaleCognition,
)
from .conversation import _event
from .conversation_contracts import ModelMessage,TextModelGateway,ModelUnavailable
from .lifecycle import TrustedPlatformActor
from .operation_contracts import (
    EffectClass,EnqueueOperation,OperationLease,Origin,Policy,Priority,
    require_claim,require_current_authority,require_idle,
)
from .operation_control import complete_operation,fail_operation
from .operation_scheduler import claim_operation,enqueue_operation
from .runtime import PermissionDenied,StaleSelection


def _range(cur,job:dict) -> tuple[dict,...]:
    """Read a frozen contiguous source range with delivery-state validation."""
    cur.execute(
        """SELECT t.turn_id,t.sequence,t.input_text,t.status,t.created_at,
                  r.response_text,r.delivery_status,
                  r.selection_generation AS response_generation
           FROM conversation_turns t
           LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
           WHERE t.conversation_id=%s AND t.user_id=%s
             AND t.instance_id=%s AND t.sequence BETWEEN %s AND %s
           ORDER BY t.sequence""",
        (job["conversation_id"],job["user_id"],job["instance_id"],
         job["start_sequence"],job["end_sequence"]),
    )
    rows=tuple(dict(x) for x in cur.fetchall())
    if len(rows)!=job["end_sequence"]-job["start_sequence"]+1 or any(
        r["sequence"]!=job["start_sequence"]+i for i,r in enumerate(rows)
    ):
        raise StaleCognition("conversation range has missing turns")
    if any(
        row["status"]=="PENDING" or (
            row["delivery_status"]=="GENERATED"
            and row["response_generation"]==job["expected_generation"]
        ) for row in rows
    ):
        raise CognitiveNotReady("source range contains pending user response")
    # Stale-generation generated assistant text is *never* evidence of delivery.
    return rows


def _digest(rows:tuple[dict,...]) -> str:
    payload=[{
        "id":str(r["turn_id"]),"sequence":r["sequence"],
        "user":r["input_text"],"status":r["status"],
        "assistant":r["response_text"] if r["delivery_status"]=="DELIVERED"
                     else None,
    } for r in rows]
    return hashlib.sha256(
        json.dumps(payload,sort_keys=True,ensure_ascii=False,
                   separators=(",",":")).encode("utf-8")
    ).hexdigest()


def schedule_cognition(
    connection:Connection,actor:TrustedPlatformActor,
    conversation_id:UUID,instance_id:UUID,expected_generation:int,
    *,retain_recent:int=1,max_turns:int=8,min_closed_turns:int=3,
    retry_failed:bool=False,
) -> CognitiveScheduled | None:
    """Serialize a safe, meaningful range; enqueue via the existing P0 runtime.

    Crash after staging but before P0 enqueue is repaired by the same call.
    A failed job is terminal: no automatic new operation is created.
    """
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor:
        raise PermissionDenied("cognitive scheduling requires trusted platform authority")
    if retry_failed and actor.source_id!="cognition.operator":
        raise PermissionDenied("terminal cognitive retry requires explicit operator authority")
    if not 1<=max_turns<=16 or not 1<=min_closed_turns<=max_turns:
        raise ValueError("invalid cognitive range policy")
    if not 1<=retain_recent<=16 or expected_generation<1:
        raise ValueError("invalid cognitive retention or generation")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT instance_id,generation,mode
                   FROM persona_selection WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if (selected is None or selected["instance_id"]!=instance_id
                or selected["generation"]!=expected_generation
                or selected["mode"] not in ("FOREGROUND","BACKGROUND")):
                raise StaleSelection("cognitive scheduling requires selected persona")
            cur.execute(
                """SELECT cognitive_watermark,next_sequence FROM conversations
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                   FOR UPDATE""",
                (conversation_id,actor.user_id,instance_id),
            )
            state=cur.fetchone()
            if state is None:
                raise PermissionDenied("cognitive conversation outside user/persona scope")
            start=state["cognitive_watermark"]+1
            cur.execute(
                """SELECT j.*,o.status AS operation_status
                   FROM cognitive_jobs j LEFT JOIN operations o
                       ON o.operation_id=j.operation_id
                   WHERE j.conversation_id=%s AND j.start_sequence=%s
                     AND j.expected_generation=%s
                   ORDER BY j.admission_attempt DESC LIMIT 1 FOR UPDATE OF j""",
                (conversation_id,start,expected_generation),
            )
            existing=cur.fetchone()
            status=existing["operation_status"] if existing is not None else None
            if status is not None and not (status=="FAILED" and retry_failed):
                # P0 owns automatic RETRY_WAIT and terminal status. An
                # ordinary scheduling tick never retries a FAILED operation.
                return None
            create_new=(existing is None or status=="FAILED")
            if create_new:
                if existing is None:
                    end=min(start+max_turns-1,
                            state["next_sequence"]-1-retain_recent)
                    if end-start+1 < min_closed_turns:
                        return None
                    attempt=1
                else:
                    # Explicit operator retry uses the frozen original range.
                    end=existing["end_sequence"]
                    attempt=existing["admission_attempt"]+1
                job={
                    "conversation_id":conversation_id,"user_id":actor.user_id,
                    "instance_id":instance_id,"expected_generation":expected_generation,
                    "start_sequence":start,"end_sequence":end,
                }
                _range(cur,job)
                op_id=uuid5(
                    NAMESPACE_URL,
                    f"persona-agent/cognition/{conversation_id}/{start}/"
                    f"{expected_generation}/{attempt}",
                )
                cur.execute(
                    """INSERT INTO cognitive_jobs(
                         operation_id,conversation_id,user_id,instance_id,
                         expected_generation,admission_attempt,
                         start_sequence,end_sequence
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (op_id,conversation_id,actor.user_id,instance_id,
                     expected_generation,attempt,start,end),
                )
            else:
                # No newer threshold may hide a staged-but-not-enqueued job.
                op_id=existing["operation_id"]
                end=existing["end_sequence"]
                attempt=existing["admission_attempt"]
                _range(cur,existing)

    receipt=enqueue_operation(connection,actor,EnqueueOperation(
        operation_id=op_id,instance_id=instance_id,
        expected_generation=expected_generation,
        idempotency_key=f"cognition:{op_id}",
        operation_type=COGNITIVE_OPERATION,
        origin=Origin.COGNITIVE,
        execution_policy=Policy.FOREGROUND_ASYNC,
        priority_class=Priority.P1,
        effect_class=EffectClass.NONE,
        resources={COGNITIVE_RESOURCE:1},
        capabilities=(COGNITIVE_CAPABILITY,),
        concurrency_group=f"cognition:{conversation_id}",
        timeout_seconds=300,max_attempts=3,
    ))
    return CognitiveScheduled(
        operation_id=op_id,conversation_id=conversation_id,
        start_sequence=start,end_sequence=end,admission_attempt=attempt,
        replayed=receipt.replayed,
    )


def prepare_cognition(connection:Connection,lease:OperationLease) -> CognitiveWork:
    """Snapshot immutable evidence outside a model or external tool call."""
    require_idle(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            op=require_claim(cur,lease)
            require_current_authority(cur,op)
            if op["operation_type"]!=COGNITIVE_OPERATION:
                raise StaleCognition("claim is not for cognitive reflection")
            cur.execute(
                """SELECT * FROM cognitive_jobs WHERE operation_id=%s
                   AND user_id=%s AND instance_id=%s""",
                (lease.operation_id,lease.user_id,lease.instance_id),
            )
            job=cur.fetchone()
            if job is None:
                raise StaleCognition("cognitive job not found")
            if job["committed_at"] is not None:
                return CognitiveWork(
                    operation_id=lease.operation_id,user_id=lease.user_id,
                    instance_id=lease.instance_id,
                    conversation_id=job["conversation_id"],
                    expected_generation=job["expected_generation"],
                    start_sequence=job["start_sequence"],end_sequence=job["end_sequence"],
                    source_digest="",turns=(),completed=True,
                )
            cur.execute(
                """SELECT cognitive_watermark FROM conversations
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s""",
                (job["conversation_id"],lease.user_id,lease.instance_id),
            )
            state=cur.fetchone()
            if state is None or state["cognitive_watermark"]!=job["start_sequence"]-1:
                raise StaleCognition("cognitive watermark has advanced")
            rows=_range(cur,job)
            return CognitiveWork(
                operation_id=lease.operation_id,user_id=lease.user_id,
                instance_id=lease.instance_id,conversation_id=job["conversation_id"],
                expected_generation=job["expected_generation"],
                start_sequence=job["start_sequence"],end_sequence=job["end_sequence"],
                source_digest=_digest(rows),turns=rows,
            )


def _messages(work:CognitiveWork) -> tuple[ModelMessage,...]:
    evidence=[{
        "turn_id":str(r["turn_id"]),"sequence":r["sequence"],
        "user":r["input_text"],
        "assistant":r["response_text"] if r["delivery_status"]=="DELIVERED"
                    else None,
    } for r in work.turns]
    instructions=(
        "Analyze the range for meaningful high-level observations, hypotheses, "
        "open questions, tentative conclusions or a short conversational-intention "
        "proposal. This is NOT chain-of-thought: do not output private reasoning. "
        "Do not invent user facts, inferred emotions or authorization. "
        "Return ONLY JSON matching {\"proposals\":[{\"kind\":"
        "\"OBSERVATION|HYPOTHESIS|CONCLUSION|QUESTION|SELF_EVALUATION|INTENTION\","
        "\"subject\":\"USER|CONVERSATION|PERSONA\",\"temporal_scope\":\"CURRENT|HISTORICAL|FUTURE\",\"content\":\"...\","
        "\"source_turn_id\":\"uuid\",\"evidence_quote\":\"verbatim user span\","
        "\"confidence\":0.5,\"trigger_text\":null,\"expiry_days\":null}]}. "
        "For intentions set trigger_text and expiry_days (1-30); otherwise null. "
        "Quote must be an exact substring of the referenced USER text. "
        "Prefer [] over unsupported inference. Maximum five items. "
        "All content remains lower-trust model interpretation, never a memory fact."
    )
    return (ModelMessage("system",instructions),
            ModelMessage("user",json.dumps(evidence,ensure_ascii=False)))


def _parse(text:str) -> CognitiveDraft:
    try:
        value=json.loads(text)
        return CognitiveDraft.model_validate(value)
    except (ValueError,TypeError,ValidationError) as exc:
        raise InvalidCognitiveProposal("invalid bounded cognitive JSON proposal") from exc


def _checked(work:CognitiveWork,draft:CognitiveDraft) -> tuple:
    by_id={r["turn_id"]:r for r in work.turns}
    seen=set()
    for item in draft.proposals:
        source=by_id.get(item.source_turn_id)
        if source is None or item.evidence_quote.casefold() not in (
            source["input_text"].casefold()
        ):
            raise InvalidCognitiveProposal("cognitive evidence must quote a source user turn")
        key=(item.kind,item.subject,item.content.casefold())
        if key in seen:
            raise InvalidCognitiveProposal("duplicate proposals in one cognitive pass")
        seen.add(key)
    return draft.proposals


def commit_cognition(
    connection:Connection,lease:OperationLease,work:CognitiveWork,
    draft:CognitiveDraft,model_id:str,
) -> CognitiveCommitted:
    """Commit source-fenced, model-interpreted candidates in a short transaction."""
    require_idle(connection)
    if not 1<=len(model_id)<=128:
        raise InvalidCognitiveProposal("invalid cognitive model identifier")
    if work.operation_id!=lease.operation_id or (
        work.user_id,work.instance_id
    )!=(lease.user_id,lease.instance_id):
        raise StaleCognition("cognitive work does not match lease")
    proposals=_checked(work,draft)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM cognitive_jobs WHERE operation_id=%s
                   AND user_id=%s AND instance_id=%s FOR UPDATE""",
                (lease.operation_id,lease.user_id,lease.instance_id),
            )
            job=cur.fetchone()
            if job is None:
                raise StaleCognition("cognitive job was removed")
            op=require_claim(cur,lease)
            require_current_authority(cur,op)
            if (job["conversation_id"]!=work.conversation_id
                or job["start_sequence"]!=work.start_sequence
                or job["end_sequence"]!=work.end_sequence):
                raise StaleCognition("cognitive range changed")
            if job["committed_at"] is not None:
                cur.execute(
                    """SELECT kind,count(*) AS n FROM cognitive_reflections
                       WHERE operation_id=%s GROUP BY kind""",
                    (lease.operation_id,),
                )
                kinds={row["kind"]:row["n"] for row in cur.fetchall()}
                return CognitiveCommitted(
                    operation_id=lease.operation_id,conversation_id=work.conversation_id,
                    end_sequence=work.end_sequence,
                    reflection_count=sum(kinds.values()),
                    intention_count=kinds.get("INTENTION",0),replayed=True,
                )
            cur.execute(
                """SELECT cognitive_watermark FROM conversations
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                   FOR UPDATE""",
                (work.conversation_id,lease.user_id,lease.instance_id),
            )
            state=cur.fetchone()
            require_claim(cur,lease)
            require_current_authority(cur,op)
            if state is None or state["cognitive_watermark"]!=work.start_sequence-1:
                raise StaleCognition("concurrent cognitive range progressed")
            rows=_range(cur,job)
            if _digest(rows)!=work.source_digest:
                raise StaleCognition("cognitive source digest changed")
            count=intentions=0
            for item in proposals:
                reflection_id=uuid4()
                # Link exact normalized repeated interpretations to their
                # original, independently cited observation. A repeated
                # model opinion is not independent evidence of its own truth.
                fingerprint=hashlib.sha256(
                    " ".join(item.content.casefold().split()).encode("utf-8")
                ).hexdigest()
                cur.execute(
                    """SELECT reflection_id FROM cognitive_reflections
                       WHERE user_id=%s AND instance_id=%s AND kind=%s
                         AND subject=%s AND content_fingerprint=%s
                       ORDER BY created_at DESC,reflection_id LIMIT 1""",
                    (lease.user_id,lease.instance_id,item.kind,item.subject,
                     fingerprint),
                )
                prior=cur.fetchone()
                related_id=prior["reflection_id"] if prior else None
                cur.execute(
                    """INSERT INTO cognitive_reflections(
                         reflection_id,operation_id,user_id,instance_id,
                         conversation_id,evidence_turn_id,related_reflection_id,
                         content_fingerprint,evidence_quote,kind,subject,
                         content,confidence,temporal_scope,model_id,
                         processed_through_sequence
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (reflection_id,lease.operation_id,lease.user_id,lease.instance_id,
                     work.conversation_id,item.source_turn_id,related_id,
                     fingerprint,item.evidence_quote,
                     item.kind,item.subject,item.content.strip(),item.confidence,
                     item.temporal_scope,model_id,work.end_sequence),
                )
                count+=1
                if item.kind=="INTENTION":
                    intentions+=1
                    cur.execute(
                        """INSERT INTO cognitive_intentions(
                             intention_id,reflection_id,user_id,instance_id,
                             trigger_text,expires_at
                           ) VALUES (%s,%s,%s,%s,%s,
                                     clock_timestamp()+(%s*interval '1 day'))""",
                        (uuid4(),reflection_id,lease.user_id,lease.instance_id,
                         item.trigger_text,item.expiry_days),
                    )
            cur.execute(
                """UPDATE conversations SET cognitive_watermark=%s,
                   updated_at=clock_timestamp()
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s""",
                (work.end_sequence,work.conversation_id,
                 lease.user_id,lease.instance_id),
            )
            cur.execute(
                """UPDATE cognitive_jobs SET committed_at=clock_timestamp(),
                   model_id=%s WHERE operation_id=%s""",
                (model_id,lease.operation_id),
            )
            _event(
                cur,user_id=lease.user_id,instance_id=lease.instance_id,
                conversation_id=work.conversation_id,
                event_type="cognition.range.reflected.v1",
                source_id="cognition.reflection",
                trust_level="TRUSTED_PLATFORM",
                detail={
                    "operation_id":str(lease.operation_id),
                    "start_sequence":work.start_sequence,
                    "end_sequence":work.end_sequence,
                    "proposals":count,"intentions":intentions,
                },
            )
            require_claim(cur,lease)
            require_current_authority(cur,op)
            return CognitiveCommitted(
                operation_id=lease.operation_id,conversation_id=work.conversation_id,
                end_sequence=work.end_sequence,reflection_count=count,
                intention_count=intentions,
            )


def run_cognition(
    connection:Connection,lease:OperationLease,gateway:TextModelGateway,
) -> CognitiveCommitted:
    """P0-leased cognition: inference runs outside any PostgreSQL transaction."""
    work=prepare_cognition(connection,lease)
    if work.completed:
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT kind,count(*) AS n FROM cognitive_reflections
                   WHERE operation_id=%s GROUP BY kind""",
                (lease.operation_id,),
            )
            counts={x["kind"]:x["n"] for x in cur.fetchall()}
        result=CognitiveCommitted(
            operation_id=lease.operation_id,conversation_id=work.conversation_id,
            end_sequence=work.end_sequence,reflection_count=sum(counts.values()),
            intention_count=counts.get("INTENTION",0),replayed=True,
        )
    else:
        try:
            generated=gateway.generate(_messages(work))
            draft=_parse(generated.text)
            result=commit_cognition(
                connection,lease,work,draft,generated.model_id,
            )
        except (InvalidCognitiveProposal,ModelUnavailable) as exc:
            # P0 owns retry/backoff and max attempts, not this model worker.
            # Failed inference does not change the cognitive watermark.
            fail_operation(connection,lease,reason=str(exc),delay_seconds=5)
            raise
    complete_operation(
        connection,lease,result_ref=f"cognition:{lease.operation_id}",
    )
    return result


def list_cognitive_proposals(
    connection:Connection,actor,instance_id:UUID,*,limit:int=20,
) -> tuple[dict,...]:
    """Read-only, user-scoped diagnostic view; never response instructions."""
    from .contracts import TrustedActor
    require_idle(connection)
    if type(actor) is not TrustedActor or not 1<=limit<=100:
        raise PermissionDenied("cognitive review requires authenticated user scope")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT r.reflection_id,r.conversation_id,r.evidence_turn_id,
                      r.evidence_quote,r.kind,r.subject,r.content,r.confidence,
                      r.related_reflection_id,r.temporal_scope,r.privacy_class,
                      r.epistemic_status,r.review_status,
                      r.processed_through_sequence,
                      r.created_at
               FROM cognitive_reflections r
               WHERE r.user_id=%s AND r.instance_id=%s
               ORDER BY r.created_at DESC,r.reflection_id LIMIT %s""",
            (actor.user_id,instance_id,limit),
        )
        return tuple(dict(x) for x in cur.fetchall())


def run_one_cognitive_operation(
    connection:Connection,gateway:TextModelGateway,*,worker_id:str,
) -> CognitiveCommitted | None:
    lease=claim_operation(
        connection,worker_id=worker_id,
        capabilities=(COGNITIVE_CAPABILITY,),
        operation_types=(COGNITIVE_OPERATION,),lease_seconds=120,
    )
    if lease is None:
        return None
    return run_cognition(connection,lease,gateway)
