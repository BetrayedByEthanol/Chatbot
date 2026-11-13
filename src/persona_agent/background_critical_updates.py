"""P4-02b — source-linked, fail-closed critical domain consolidation.

This is a deterministic, bounded integration with existing Memory/Goal/Task
owners. It NEVER accepts a CognitiveProposal as a command or treats an LLM
reflection as an authoritative domain update.

The recognizers below are conservative sentinels for clear user requests.
They are not a semantic classification oracle. Unhandled explicit restrictions
remain pending and cannot authorize Background autonomy. P4-03 is responsible
for the broader change-focused validation and final P2 readiness barrier.
"""
import re
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .memory_extraction import parse_explicit_claim
from .operation_contracts import require_idle
from .runtime import PermissionDenied


_SCAN_BATCH=24
_REVIEW_BATCH=24

# A model output or quoted conversation is never an authenticated instruction.
# Only original, scoped user input is passed through these conservative guards.
_GOAL=re.compile(
    r"^\s*(?:please\s+)?(?:pause|resume|abandon|cancel|change|replace|stop)"
    r"\s+(?:my|our|the)\s+(?:goal|plan)\b",re.I,
)
_TASK=re.compile(
    r"^\s*(?:please\s+)?(?:prioriti[sz]e|suspend|resume|cancel|reorder|"
    r"reschedule)\s+(?:(?:my|our|the|this)\s+)?tasks?\b",re.I,
)
_INSTRUCTION=re.compile(
    r"^\s*(?:please\s+)?(?:do\s+not|don't|never|stop|disable|cancel)"
    r"\s+(?:any\s+|all\s+)?(?:run\s+|running\s+)?"
    r"(?:background|autonomous|autonomy|tools?|executions?|"
    r"notifications?|reminders?)\b",re.I,
)

def _classify(source_text: str) -> tuple[str,...]:
    """Potentially consequential direct user assertions, not model proposals."""
    categories=[]
    if parse_explicit_claim(source_text) is not None:
        categories.append("MEMORY")
    if _GOAL.search(source_text):
        categories.append("GOAL")
    if _TASK.search(source_text):
        categories.append("TASK")
    if _INSTRUCTION.search(source_text):
        categories.append("INSTRUCTION")
    return tuple(categories)


def _owner_reference(cur, row: dict) -> UUID | None:
    """Read a domain-owned, source-linked decision, never an arbitrary receipt."""
    params=(row["user_id"],row["instance_id"],
            row["conversation_id"],row["source_turn_id"])
    if row["domain"]=="MEMORY":
        cur.execute(
            """SELECT candidate_id,status FROM memory_extraction_candidates
               WHERE user_id=%s AND instance_id=%s AND conversation_id=%s
                 AND source_turn_id=%s
               ORDER BY candidate_id LIMIT 1""",params,
        )
        candidate=cur.fetchone()
        # CONFLICT requires the authenticated Memory owner's review/correction;
        # STALE is an explicit Memory-domain rejection, not a new fact.
        if candidate is not None and candidate["status"] in (
            "ADMITTED","CORROBORATED","STALE","APPROVED"
        ):
            return candidate["candidate_id"]
        return None
    if row["domain"]=="GOAL":
        cur.execute(
            """SELECT proof.event_id FROM (
                 SELECT event_id,user_id,instance_id,source_conversation_id,
                        source_turn_id FROM goal_status_revisions
                 UNION ALL
                 SELECT created_event_id,user_id,instance_id,
                        source_conversation_id,source_turn_id FROM goal_records
                 UNION ALL
                 SELECT event_id,user_id,instance_id,
                        source_conversation_id,source_turn_id
                 FROM goal_strategy_selections
               ) proof JOIN events e ON e.event_id=proof.event_id
                    AND e.user_id=proof.user_id AND e.instance_id=proof.instance_id
               WHERE proof.user_id=%s AND proof.instance_id=%s
                 AND proof.source_conversation_id=%s AND proof.source_turn_id=%s
                 AND e.trust_level='AUTHENTICATED_USER'
               ORDER BY proof.event_id LIMIT 1""",params,
        )
        found=cur.fetchone()
        return found["event_id"] if found else None
    if row["domain"]=="TASK":
        cur.execute(
            """SELECT p.event_id FROM task_priority_rules p
               JOIN events e ON e.event_id=p.event_id
                 AND e.user_id=p.user_id AND e.instance_id=p.instance_id
               WHERE p.user_id=%s AND p.instance_id=%s
                 AND p.source_conversation_id=%s AND p.source_turn_id=%s
                 AND e.trust_level='AUTHENTICATED_USER'
               ORDER BY p.event_id LIMIT 1""",params,
        )
        found=cur.fetchone()
        if found:
            return found["event_id"]
        # Task cancellation is recorded as a Task-owned command/event; unlike
        # priority rules it has no separate revision table. Require its exact
        # receipt, the authenticated source turn, and the resulting CANCELLED
        # task projection, rather than trusting an arbitrary event payload.
        cur.execute(
            """SELECT e.event_id FROM events e
               JOIN command_receipts receipt
                 ON receipt.event_id=e.event_id
                AND receipt.user_id=e.user_id
                AND receipt.instance_id=e.instance_id
                AND receipt.command_type='goal.task.cancel.v1'
               JOIN goal_tasks t ON t.user_id=e.user_id
                AND t.instance_id=e.instance_id
                AND t.task_id::text=e.payload->>'task_id'
                AND t.status='CANCELLED'
               WHERE e.user_id=%s AND e.instance_id=%s
                 AND e.event_type='goal.task.cancelled.v1'
                 AND e.trust_level='AUTHENTICATED_USER'
                 AND e.payload->>'source_turn_id'=%s
               ORDER BY e.recorded_at DESC LIMIT 1""",
            (row["user_id"],row["instance_id"],str(row["source_turn_id"])),
        )
        cancelled=cur.fetchone()
        return cancelled["event_id"] if cancelled else None
    # No general authoritative instruction/permission domain is installed.
    # Never accept a platform boolean or a conversational guess as approval.
    return None


def process_critical_updates(cur, epoch: dict) -> str:
    """Run within the coordinator's selected-epoch locked transaction.

    Returns SCANNING, VERIFYING, WAITING_OWNER or ALIGNMENT_PENDING.
    Up to one small source range + one bounded review batch per transaction.
    """
    scope=(epoch["user_id"],epoch["instance_id"],epoch["background_epoch"])
    cur.execute(
        """SELECT b.* FROM background_preparation_conversations b
           WHERE b.user_id=%s AND b.instance_id=%s AND b.background_epoch=%s
             AND b.critical_scan_sequence < b.target_sequence
           ORDER BY b.conversation_id LIMIT 1 FOR UPDATE""",scope,
    )
    batch=cur.fetchone()
    if batch is not None:
        cur.execute(
            """SELECT turn_id,sequence,input_text FROM conversation_turns
               WHERE user_id=%s AND instance_id=%s AND conversation_id=%s
                 AND sequence>%s AND sequence<=%s
               ORDER BY sequence LIMIT %s""",
            (epoch["user_id"],epoch["instance_id"],batch["conversation_id"],
             batch["critical_scan_sequence"],batch["target_sequence"],_SCAN_BATCH),
        )
        rows=cur.fetchall()
        if not rows or rows[0]["sequence"]!=batch["critical_scan_sequence"]+1:
            # Keep the stage non-ready; an inconsistent source is not a
            # successful empty review. Coordinator marks BLOCKED separately.
            return "SOURCE_GAP"
        for index,source in enumerate(rows):
            if source["sequence"]!=batch["critical_scan_sequence"]+index+1:
                return "SOURCE_GAP"
            for domain in _classify(source["input_text"]):
                cur.execute(
                    """INSERT INTO background_critical_updates(
                         user_id,instance_id,background_epoch,conversation_id,
                         source_turn_id,source_sequence,domain
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (*scope,batch["conversation_id"],source["turn_id"],
                     source["sequence"],domain),
                )
        cur.execute(
            """UPDATE background_preparation_conversations
               SET critical_scan_sequence=%s
               WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                 AND conversation_id=%s AND critical_scan_sequence=%s""",
            (rows[-1]["sequence"],*scope,batch["conversation_id"],
             batch["critical_scan_sequence"]),
        )
        if cur.rowcount!=1:
            return "SOURCE_GAP"
        return "SCANNING"

    cur.execute(
        """SELECT * FROM background_critical_updates
           WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
             AND status='PENDING'
           ORDER BY conversation_id,source_sequence,domain LIMIT %s""",
        (*scope,_REVIEW_BATCH),
    )
    pending=cur.fetchall()
    progressed=0
    for issue in pending:
        owner_ref=_owner_reference(cur,issue)
        if owner_ref is None:
            continue
        cur.execute(
            """UPDATE background_critical_updates
               SET status='VERIFIED',owner_ref=%s,decided_at=clock_timestamp()
               WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                 AND source_turn_id=%s AND domain=%s AND status='PENDING'""",
            (owner_ref,*scope,issue["source_turn_id"],issue["domain"]),
        )
        progressed+=cur.rowcount
    if pending:
        return "VERIFYING" if progressed else "WAITING_OWNER"

    # No staged input remains and no unresolved owner decisions were observed
    # in the same transaction. Advance each conversation's owned watermark
    # for this epoch only. A selected-persona return must wait for this txn.
    cur.execute(
        """UPDATE conversations c
           SET critical_committed_watermark=GREATEST(
                     c.critical_committed_watermark,b.target_sequence)
           FROM background_preparation_conversations b
           WHERE b.user_id=%s AND b.instance_id=%s AND b.background_epoch=%s
             AND c.conversation_id=b.conversation_id
             AND c.user_id=b.user_id AND c.instance_id=b.instance_id
             AND b.critical_scan_sequence=b.target_sequence""",scope,
    )
    return "ALIGNMENT_PENDING"


def list_critical_updates(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    background_epoch: int, *, limit: int=100,
) -> tuple[dict,...]:
    """Authenticated evidence-backed review queue; never mutate owner state."""
    require_idle(connection)
    if type(actor) is not TrustedActor or not 1<=limit<=200:
        raise PermissionDenied("critical review requires authenticated user")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s""",
            (actor.user_id,instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("critical review outside persona scope")
        cur.execute(
            """SELECT conversation_id,source_turn_id,source_sequence,domain,
                      status,owner_ref FROM background_critical_updates
               WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
               ORDER BY conversation_id,source_sequence,domain LIMIT %s""",
            (actor.user_id,instance_id,background_epoch,limit),
        )
        return tuple(dict(x) for x in cur.fetchall())
