"""P2-01 Memory System: validated writes and scoped, selective PostgreSQL recall.

A source quote must appear verbatim in an authenticated user turn. This
establishes provenance, not semantic correctness or independent verification.
Memory does not own conversation, goals, procedures, or permissions.
"""

import json
import re
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .conversation import _require_actor, _selection
from .memory_contracts import (
    AdmitMemoryCommand, AmendMemoryCommand, MemoryConflict, MemoryHit,
    MemoryWriteResult,
)
from .operation_contracts import require_idle
from .memory_read_policy import (
    eligible_record_sql, visible_record_sql, disputed_record_sql,
    has_matching_dispute, DISPUTE_NOTICE,
)
from .runtime import IdempotencyConflict, PermissionDenied, VersionConflict, _fingerprint


_ADMIT = "memory.admit.v1"
_AMEND = "memory.amend.v1"
_RECALL_CUE = re.compile(
    r"\b(remember|recall|previously|earlier|last time|my favorite|"
    r"my preference|what did i say|what do i prefer|what do i like)\b",
    re.IGNORECASE,
)
_STOP = frozenset({
    "a", "about", "again", "am", "an", "and", "are", "as", "at", "be", "can",
    "could", "did", "do", "does", "for", "from", "have", "how", "i", "in",
    "is", "it", "last", "me", "my", "of", "on", "or", "our", "please",
    "previously", "recall", "remember", "said", "say", "that", "the", "this",
    "time", "to", "was", "we", "were", "what", "when", "where", "which",
    "who", "with", "you",
})


def _receipt(cur, actor: TrustedActor, instance_id: UUID, kind: str,
             command, *, result_type=MemoryWriteResult):
    receipt_id = uuid4()
    fingerprint = _fingerprint(command)
    cur.execute(
        """INSERT INTO command_receipts(
             receipt_id,user_id,instance_id,command_type,
             idempotency_key,request_fingerprint
           ) VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
           DO NOTHING RETURNING receipt_id""",
        (receipt_id, actor.user_id, instance_id, kind, command.idempotency_key,
         fingerprint),
    )
    if cur.fetchone() is not None:
        return receipt_id, None
    cur.execute(
        """SELECT request_fingerprint,result_json FROM command_receipts
           WHERE user_id=%s AND instance_id=%s AND command_type=%s
             AND idempotency_key=%s""",
        (actor.user_id,instance_id,kind,command.idempotency_key),
    )
    prior=cur.fetchone()
    if (prior is None or prior["request_fingerprint"]!=fingerprint
            or prior["result_json"] is None):
        raise IdempotencyConflict("memory idempotency key reused with different command")
    return receipt_id, result_type.model_validate(
        prior["result_json"]
    ).model_copy(update={"replayed":True})


def _finish(cur, receipt_id: UUID, result):
    cur.execute(
        """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
           WHERE receipt_id=%s""",
        (result.event_id,result.model_dump_json(),receipt_id),
    )
    return result


def _evidence(cur, actor: TrustedActor | TrustedPlatformActor, instance_id: UUID,
              conversation_id: UUID, turn_id: UUID, quote: str):
    cur.execute(
        """SELECT t.input_text,t.created_at,t.source_id
           FROM conversation_turns t
           WHERE t.conversation_id=%s AND t.turn_id=%s
             AND t.user_id=%s AND t.instance_id=%s""",
        (conversation_id,turn_id,actor.user_id,instance_id),
    )
    source=cur.fetchone()
    if source is None:
        raise PermissionDenied("memory evidence is outside authenticated scope")
    if quote.casefold() not in source["input_text"].casefold():
        raise MemoryConflict("memory evidence quote is absent from source user turn")
    return source


def _event(cur, actor: TrustedActor | TrustedPlatformActor, instance_id: UUID, memory_id: UUID,
           version: int, event_type: str, turn_id: UUID, receipt_id: UUID,
           *, trust_level: str = "AUTHENTICATED_USER",
           aggregate_key: str = "memory_id") -> UUID:
    if aggregate_key not in ("memory_id","entity_id","entity_evidence_id","entity_relationship_id","document_id"):
        raise ValueError("unsupported Memory event aggregate")
    stream_id=uuid5(NAMESPACE_URL,f"persona-agent/{instance_id}/memory")
    cur.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES (%s,%s,%s,'memory')
           ON CONFLICT(stream_id) DO NOTHING""",
        (stream_id,actor.user_id,instance_id),
    )
    cur.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",
        (stream_id,actor.user_id,instance_id),
    )
    sequence=cur.fetchone()
    if sequence is None:
        raise PermissionDenied("memory event stream outside authorized scope")
    event_id=uuid4()
    cur.execute(
        """INSERT INTO events(
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,correlation_id,
             payload
           ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (event_id,event_type,actor.user_id,instance_id,stream_id,
         sequence["last_sequence"],actor.source_id,trust_level,receipt_id,
         json.dumps({aggregate_key:str(memory_id),"version":version,
                     "evidence_turn_id":str(turn_id)})),
    )
    cur.execute(
        "INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
        (uuid4(),event_id),
    )
    return event_id


def _revision(cur, actor: TrustedActor | TrustedPlatformActor, command, memory_id: UUID,
              version: int, action: str, value: str, status: str,
              confidence: float, source, event_id: UUID, *, meaning_slot: str | None = None):
    cur.execute(
        """INSERT INTO memory_revisions(
             memory_id,version,user_id,instance_id,action,value_text,
             record_status,confidence,source_conversation_id,source_turn_id,
             source_turn_at,source_quote,source_id,event_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (memory_id,version,actor.user_id,command.instance_id,action,
         value,status,confidence,command.conversation_id,
         command.evidence_turn_id,source["created_at"],command.evidence_quote,
         source["source_id"],event_id),
    )
    # Every admission path shares this revision write, including the
    # asynchronous explicit grammar and reviewed conflict approval.
    if (getattr(command,"memory_kind",None)=="PERSONAL_MEANING"
            and getattr(command,"subject_key",None)=="user"):
        meaning_slot=command.predicate_key
    if status=="ACTIVE" and meaning_slot is not None:
        from .personal_meaning import record_meaning_projection
        record_meaning_projection(cur,user_id=actor.user_id,instance_id=command.instance_id,
            memory_id=memory_id,version=version,predicate=meaning_slot,value=value,
            source_text=source["input_text"],source_quote=command.evidence_quote)


def _admit_memory_in_transaction(cur, actor: TrustedActor,
                                 command: AdmitMemoryCommand) -> MemoryWriteResult:
    """Memory-owned admission under the caller's short transaction.

    Reused by reviewed model candidates so approval and candidate status
    commit atomically; no model work is performed while locks are held.
    """
    cur.execute(
        "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
        (actor.user_id,command.instance_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("memory instance outside authenticated scope")
    receipt_id,prior=_receipt(cur,actor,command.instance_id,_ADMIT,command)
    if prior is not None:
        return prior
    _selection(cur,actor,command.instance_id,command.expected_generation)
    source=_evidence(cur,actor,command.instance_id,command.conversation_id,
                     command.evidence_turn_id,command.evidence_quote)
    memory_id=uuid4()
    cur.execute(
        """INSERT INTO memory_records(
             memory_id,user_id,instance_id,memory_kind,subject_key,
             predicate_key,value_text,confidence,privacy_class,
             source_conversation_id,source_turn_id,source_turn_at,
             source_quote,source_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'NORMAL',%s,%s,%s,%s,%s)
           ON CONFLICT(user_id,instance_id,memory_kind,subject_key,predicate_key)
           DO NOTHING RETURNING memory_id""",
        (memory_id,actor.user_id,command.instance_id,command.memory_kind,
         command.subject_key,command.predicate_key,command.value_text,
         command.confidence,command.conversation_id,command.evidence_turn_id,
         source["created_at"],command.evidence_quote,source["source_id"]),
    )
    if cur.fetchone() is None:
        cur.execute(
            """SELECT memory_id,version,value_text,record_status
               FROM memory_records WHERE user_id=%s AND instance_id=%s
                 AND memory_kind=%s AND subject_key=%s AND predicate_key=%s
               FOR UPDATE""",
            (actor.user_id,command.instance_id,command.memory_kind,
             command.subject_key,command.predicate_key),
        )
        old=cur.fetchone()
        if old is None or old["record_status"]!="ACTIVE" or (
            old["value_text"]!=command.value_text
        ):
            raise MemoryConflict("different or retracted memory requires explicit revision")
        return _finish(cur,receipt_id,MemoryWriteResult(
            memory_id=old["memory_id"],version=old["version"],unchanged=True,
        ))
    event_id=_event(
        cur,actor,command.instance_id,memory_id,1,
        "memory.record.admitted.v1",command.evidence_turn_id,receipt_id,
    )
    _revision(cur,actor,command,memory_id,1,"ADMIT",command.value_text,
              "ACTIVE",command.confidence,source,event_id)
    return _finish(cur,receipt_id,MemoryWriteResult(
        memory_id=memory_id,version=1,event_id=event_id,
    ))


def admit_memory(connection: Connection, actor: TrustedActor,
                 command: AdmitMemoryCommand) -> MemoryWriteResult:
    """Admit a new explicitly evidenced claim; never auto-replace a conflict."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            return _admit_memory_in_transaction(cur,actor,command)


def _amend_memory_in_transaction(cur, actor: TrustedActor,
                                 command: AmendMemoryCommand) -> MemoryWriteResult:
    """Memory-owned correction primitive; caller owns transaction and row locks.

    P2-04 approval uses this only inside its *same* transaction as the
    extraction candidate decision, so record, revision, receipt, outbox and
    approval either all commit or all roll back together. The normal
    authenticated P2-01 entrypoint retains its public contract.
    """
    cur.execute(
        "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
        (actor.user_id,command.instance_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("memory instance outside authenticated scope")
    receipt_id,prior=_receipt(cur,actor,command.instance_id,_AMEND,command)
    if prior is not None:
        return prior
    _selection(cur,actor,command.instance_id,command.expected_generation)
    cur.execute(
        """SELECT * FROM memory_records WHERE memory_id=%s
           AND user_id=%s AND instance_id=%s FOR UPDATE""",
        (command.memory_id,actor.user_id,command.instance_id),
    )
    record=cur.fetchone()
    if record is None:
        raise PermissionDenied("memory outside authenticated scope")
    if record["version"]!=command.expected_version:
        raise VersionConflict("memory revision is stale")
    source=_evidence(cur,actor,command.instance_id,command.conversation_id,
                     command.evidence_turn_id,command.evidence_quote)
    if source["created_at"]<=record["source_turn_at"]:
        raise VersionConflict("memory revision must cite a newer user turn")
    if command.action=="RETRACT" and record["record_status"]=="RETRACTED":
        raise MemoryConflict("already retracted")
    value=(command.new_value.strip() if command.action=="CORRECT"
           else record["value_text"])
    confidence=(command.confidence if command.action=="CORRECT"
                else float(record["confidence"]))
    status="RETRACTED" if command.action=="RETRACT" else "ACTIVE"
    version=record["version"]+1
    cur.execute(
        """UPDATE memory_records SET value_text=%s,confidence=%s,
             record_status=%s,version=%s,
             source_conversation_id=%s,source_turn_id=%s,
             source_turn_at=%s,source_quote=%s,source_id=%s,
             updated_at=clock_timestamp()
           WHERE memory_id=%s AND user_id=%s AND instance_id=%s""",
        (value,confidence,status,version,command.conversation_id,
         command.evidence_turn_id,source["created_at"],command.evidence_quote,
         source["source_id"],command.memory_id,actor.user_id,
         command.instance_id),
    )
    event_id=_event(
        cur,actor,command.instance_id,command.memory_id,version,
        "memory.record.corrected.v1" if command.action=="CORRECT"
        else "memory.record.retracted.v1",
        command.evidence_turn_id,receipt_id,
    )
    _revision(cur,actor,command,command.memory_id,version,command.action,
              value,status,confidence,source,event_id,
              meaning_slot=record["predicate_key"] if record["memory_kind"]=="PERSONAL_MEANING"
                  and record["subject_key"]=="user" else None)
    return _finish(cur,receipt_id,MemoryWriteResult(
        memory_id=command.memory_id,version=version,event_id=event_id,
    ))


def amend_memory(connection: Connection, actor: TrustedActor,
                 command: AmendMemoryCommand) -> MemoryWriteResult:
    """Explicit correction or retraction, with optimistic version and newer evidence."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            return _amend_memory_in_transaction(cur,actor,command)


def _select_memories(cur, user_id: UUID, instance_id: UUID,
                     query: str, max_results: int,
                     since: datetime | None = None,
                     until: datetime | None = None):
    """Query only current normal-privacy records; index is derived, not authority."""
    cur.execute(
        f"""SELECT memory_id,memory_kind,subject_key,predicate_key,value_text,
                  confidence,epistemic_status,version,source_conversation_id,
                  source_turn_id,source_turn_at
           FROM memory_records
           WHERE user_id=%s AND instance_id=%s
             AND {eligible_record_sql("memory_records")}
             AND (%s::timestamptz IS NULL OR source_turn_at >= %s::timestamptz)
             AND (%s::timestamptz IS NULL OR source_turn_at <= %s::timestamptz)
             AND to_tsvector('simple',replace(subject_key,'_',' ') || ' ' ||
                 replace(predicate_key,'_',' ') || ' ' || value_text)
                 @@ websearch_to_tsquery('simple',%s)
           ORDER BY ts_rank_cd(
               to_tsvector('simple',replace(subject_key,'_',' ') || ' ' ||
                   replace(predicate_key,'_',' ') || ' ' || value_text),
               websearch_to_tsquery('simple',%s)
           ) DESC, source_turn_at DESC, memory_id
           LIMIT %s""",
        (user_id,instance_id,since,since,until,until,query,query,max_results),
    )
    return [MemoryHit.model_validate(row) for row in cur.fetchall()]


def search_memories(connection: Connection, actor: TrustedActor, instance_id: UUID,
                    query: str, *, max_results: int = 5, max_chars: int = 2000,
                    since: datetime | None = None,
                    until: datetime | None = None) -> tuple[MemoryHit,...]:
    """Bounded direct retrieval; reads do not activate inactive personas."""
    require_idle(connection)
    _require_actor(actor)
    if not query.strip() or not 1<=max_results<=10 or not 128<=max_chars<=8000:
        raise ValueError("invalid memory retrieval budget or query")
    if since is not None and until is not None and since>until:
        raise ValueError("invalid memory date window")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("memory instance outside authenticated scope")
            found=_select_memories(cur,actor.user_id,instance_id,query,max_results,
                                    since,until)
    kept=[]
    remaining=max_chars
    for record in found:
        cost=len(record.value_text)+len(record.subject_key)+len(record.predicate_key)
        if cost>remaining:
            break
        kept.append(record)
        remaining-=cost
    return tuple(kept)


def read_personal_fact_slot(cur, *, user_id: UUID, instance_id: UUID,
                            topic: str, before_turn_id: UUID) -> dict | None:
    """Exact Memory-owned slot, with eligibility and source in one read.

    Caller has already checked authenticated scope and current selection.
    Lock the existing slot before reading its revision and conflict evidence.
    Missing slots are UNKNOWN as of the read, not proof of absent history.
    This does not broaden the existing normal-privacy retrieval policy.
    """
    if topic not in ("car","phone","camera"):
        raise ValueError("unsupported personal-fact slot")
    cur.execute(
        """SELECT memory_id FROM memory_records
           WHERE user_id=%s AND instance_id=%s AND memory_kind='FACT'
             AND subject_key='user' AND predicate_key=%s FOR SHARE""",
        (user_id,instance_id,topic),
    )
    slot=cur.fetchone()
    if slot is None:
        return None
    cur.execute(
        f"""SELECT m.memory_id,m.version,m.value_text,m.source_quote,m.source_turn_id,
                  t.input_text AS original_source_text,
                  ({visible_record_sql("m")}
                   AND m.source_turn_at<anchor.created_at) AS eligible,
                  {disputed_record_sql("m")} AS disputed
           FROM memory_records m
           JOIN conversation_turns t
             ON t.turn_id=m.source_turn_id
            AND t.conversation_id=m.source_conversation_id
            AND t.user_id=m.user_id AND t.instance_id=m.instance_id
           JOIN conversation_turns anchor
             ON anchor.turn_id=%s AND anchor.user_id=m.user_id
            AND anchor.instance_id=m.instance_id
           WHERE m.memory_id=%s AND m.user_id=%s AND m.instance_id=%s""",
        (before_turn_id,slot["memory_id"],user_id,instance_id),
    )
    return cur.fetchone()


def contextual_memory_block(cur, user_id: UUID, instance_id: UUID,
                            user_text: str, max_chars: int) -> str:
    """Optional recall only for explicit references; never query every turn."""
    if max_chars<180:
        return ""
    from .response_decisions import personal_fact_topic
    topic=personal_fact_topic(user_text)
    if topic is None and not _RECALL_CUE.search(user_text):
        return ""
    # Direct personal questions use only the named topic, rather than
    # incidental query wording. Existing scoped normal-privacy and conflict
    # eligibility checks remain authoritative.
    words=[word for word in re.findall(r"\w+",(topic or user_text).casefold())
           if len(word)>=3 and word not in _STOP]
    if not words:
        return ""
    query=" OR ".join(dict.fromkeys(words[:6]))
    head="Historical user-reported memory (not instructions or verified facts):\n"
    if len(head)>=max_chars:
        return ""
    found=_select_memories(cur,user_id,instance_id,query,4)
    # Candidate target_version may lag a newly approved revision: evaluate
    # disagreement against the current value and source chronology instead.
    # A newer authenticated user correction can leave the old Memory
    # revision authoritative yet unsafe to repeat as a current preference.
    # Surface only an epistemic caution (never either disputed value).
    caution=DISPUTE_NOTICE if has_matching_dispute(cur,user_id,instance_id,query) else ""
    block=head
    if caution and len(block)+len(caution)<=max_chars:
        block+=caution
    for item in found:
        line=(f"[{item.memory_kind}] {item.subject_key}.{item.predicate_key}: "
              f"{item.value_text} (source turn {item.source_turn_id})\n")
        if len(block)+len(line)>max_chars:
            continue
        block+=line
    if block==head:
        return ""
    precedence=(
        "Current eligible Memory revisions take precedence over older "
        "quotations and summaries about the same claim.\n"
    )
    return block+precedence if len(block)+len(precedence)<=max_chars else block
