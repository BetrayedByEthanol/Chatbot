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

# Only complete, first-person, single-sentence assertions. Broader natural-
# language extraction requires an independently validated model-candidate gate.
_FAVORITE = re.compile(r"my favorite ([a-z][a-z -]{1,30}) is (.+)", re.I)
_HOBBY = re.compile(r"my hobby is (.+)", re.I)
_PET = re.compile(r"my (dog|cat)(?:'s name is| is named) (.+)", re.I)
_MEANING = re.compile(r"for me,? ([a-z][a-z -]{1,30}) means (.+)", re.I)
_PREFER = re.compile(r"i prefer (.+?) over ([a-z][a-z -]{1,30})", re.I)
_BLOCKED = re.compile(
    r"\b(address|password|pin|secret|ssn|bank|credit|account|salary|income|"
    r"health|medical|diagnos\w*|disease|medication|pregnan\w*|therapy|"
    r"religio\w*|politic\w*|ethnic\w*|sexual\w*|gender|birthday|"
    r"birthdate|age|phone|email|postcode|zip code|location|"
    r"maybe|perhaps|probably|sometimes|used to|not really|"
    r"someone|hypothetic\w*|for example|joking|sarcas\w*)\b",
    re.I,
)
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Positive, deliberately small, non-sensitive domain vocabulary. A syntax
# match alone never authorizes admission as NORMAL privacy memory. Expand
# categories only with privacy evaluation and regression evidence.
_SAFE_FAVORITES = {
    "drink": frozenset({"tea","coffee","water","milk","juice","lemonade"}),
    "food": frozenset({"pizza","pasta","soup","salad","rice","bread","sushi"}),
    "color": frozenset({"red","blue","green","yellow","orange","purple",
                        "pink","black","white","brown","grey","gray"}),
}
_SAFE_HOBBIES = frozenset({
    "gardening","cycling","reading","painting","drawing","knitting",
    "hiking","swimming","photography","cooking","baking","chess",
})
_SAFE_PET_NAMES = frozenset({
    "milo","luna","bella","max","charlie","buddy","daisy","coco",
    "rocky","lucy","oliver","leo","molly",
})
_SAFE_MEANINGS = {
    "home": frozenset({"feeling safe","belonging","comfort"}),
    "friendship": frozenset({"trust","support","companionship"}),
    "learning": frozenset({"curiosity","growth","exploration"}),
}
_SAFE_PREFERENCES = frozenset().union(
    *tuple(_SAFE_FAVORITES.values()),_SAFE_HOBBIES,
)


def _supported_safe_claim(kind: str, predicate: str, value: str) -> bool:
    """Positive allowlist for BOTH the subject topic and its reported value.

    Never use a blacklist or a free-text syntax match as authorization to
    write NORMAL-privacy authoritative memories.
    """
    term=value.casefold()
    if kind=="PREFERENCE" and predicate.startswith("favorite_"):
        topic=predicate.removeprefix("favorite_")
        return term in _SAFE_FAVORITES.get(topic,())
    if kind=="PREFERENCE" and predicate.startswith("prefer_over_"):
        alternative=predicate.removeprefix("prefer_over_").replace("_"," ")
        return term in _SAFE_PREFERENCES and alternative in _SAFE_PREFERENCES
    if kind=="FACT" and predicate=="hobby":
        return term in _SAFE_HOBBIES
    if kind=="FACT" and predicate in ("pet_dog_name","pet_cat_name"):
        return term in _SAFE_PET_NAMES
    if kind=="PERSONAL_MEANING" and predicate.startswith("meaning_"):
        topic=predicate.removeprefix("meaning_")
        return term in _SAFE_MEANINGS.get(topic,())
    return False


@dataclass(frozen=True)
class ExplicitClaim:
    memory_kind: str
    subject_key: str
    predicate_key: str
    value_text: str
    source_quote: str
    confidence: float = 0.97


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


def _slug(value: str) -> str:
    return re.sub(r"\s+", "_", value.strip().casefold())


def parse_explicit_claim(text: str) -> ExplicitClaim | None:
    """High-precision grammar, not a semantic fact verifier.

    Accept one short declarative self-report and preserve the exact source
    quote. Quotes, multiple sentences, questions, sensitive or speculative
    phrasing, and unsupported topics are deliberately left unextracted.
    """
    statement=text.strip()
    if (len(statement)>320 or len(statement)<8
        or any(c in statement for c in ('\n','\r','"',"'",";",":","?","!","[","]","{","}","<",">"))
        or _BLOCKED.search(statement)):
        # Pet possessives with an apostrophe are handled below; other quoted
        # or possessive language is too ambiguous for this first-pass parser.
        if not (statement.casefold().startswith(("my dog's name is ",
                                                 "my cat's name is "))
                and not _BLOCKED.search(statement)
                and all(c not in statement for c in ('\n','\r','"',';',':','?','!','[',']','{','}','<','>'))
                and len(statement)<=320):
            return None
    normalized=re.sub(r"^actually,?\s+", "", statement, flags=re.I)
    if normalized.endswith("."):
        normalized=normalized[:-1]
    # Permit only the single optional "For me," introduction comma.
    if "." in normalized or "," in re.sub(r"^for me,", "", normalized, flags=re.I):
        return None
    patterns=(
        (_FAVORITE,"PREFERENCE","favorite_"),
        (_HOBBY,"FACT","hobby"),
        (_PET,"FACT","pet_"),
        (_MEANING,"PERSONAL_MEANING","meaning_"),
        (_PREFER,"PREFERENCE","prefer_over_"),
    )
    for pattern,kind,stem in patterns:
        match=pattern.fullmatch(normalized)
        if match is None:
            continue
        if pattern is _FAVORITE:
            predicate=stem+_slug(match[1])
            value=match[2].strip()
        elif pattern is _HOBBY:
            predicate=stem
            value=match[1].strip()
        elif pattern is _PET:
            predicate=f"pet_{match[1].casefold()}_name"
            value=match[2].strip()
        elif pattern is _MEANING:
            predicate=stem+_slug(match[1])
            value=match[2].strip()
        else:
            predicate=stem+_slug(match[2])
            value=match[1].strip()
        if (not _KEY.fullmatch(predicate) or not 1<=len(value)<=100
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &/_-]*",value)
            or re.search(r"\b(and|but|because|unless|although|however)\b",value,re.I)
            or not _supported_safe_claim(kind,predicate,value)):
            continue
        return ExplicitClaim(kind,"user",predicate,value,statement)
    return None


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
