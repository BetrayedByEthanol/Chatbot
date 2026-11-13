"""P4-02: strict, non-authoritative Background handover coordinator.

One existing P0 scheduler performs model inference and worker claims. This
coordinator only snapshots/checks the frozen epoch, starts existing P2 summary
and cognitive operations, and records ordered preparation progress.

CRITICAL_PENDING is intentionally NOT readiness. Only P4-03's owner-validated
critical commits + ALIGN-01 gate may insert into background_ready_epochs.
"""

from dataclasses import dataclass
import json
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .cognitive_contracts import CognitiveNotReady, StaleCognition
from .cognitive_engine import schedule_cognition
from .lifecycle import TrustedPlatformActor
from .operation_contracts import require_idle
from .runtime import PermissionDenied, StaleSelection
from .summary_contracts import SummaryNotReady, StaleSummary
from .summary_memory import schedule_summary

_COORDINATOR = "background.preparation"
_TERMINAL = frozenset(("FAILED", "CANCELLED", "BLOCKED", "OUTCOME_UNKNOWN"))
_IN_PROGRESS = frozenset(("QUEUED", "RUNNING", "RETRY_WAIT",
                          "CANCEL_REQUESTED", "SUSPEND_REQUESTED", "SUSPENDED"))


class StaleBackgroundPreparation(Exception):
    """The selected persona or prepared epoch is no longer authoritative."""


@dataclass(frozen=True)
class PreparationProgress:
    stage: str
    state: str
    conversation_id: UUID | None = None
    operation_id: UUID | None = None


def _event(cursor, epoch: dict, old_stage: str, new_stage: str,
           *, reason: str | None = None) -> None:
    """Append an event + outbox in the same transaction as stage mutation."""
    stream_id = uuid5(
        NAMESPACE_URL,
        f"persona-agent/background-preparation/{epoch['instance_id']}",
    )
    cursor.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES (%s,%s,%s,'background_preparation')
           ON CONFLICT(stream_id) DO NOTHING""",
        (stream_id, epoch["user_id"], epoch["instance_id"]),
    )
    cursor.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",
        (stream_id, epoch["user_id"], epoch["instance_id"]),
    )
    seq = cursor.fetchone()
    if seq is None:
        raise PermissionDenied("preparation stream outside authorized scope")
    event_id = uuid4()
    cursor.execute(
        """INSERT INTO events (
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,payload
           ) VALUES (%s,'background.preparation.stage_changed.v1',1,%s,%s,
                     %s,%s,%s,'TRUSTED_PLATFORM',%s::jsonb)""",
        (event_id,epoch["user_id"],epoch["instance_id"],stream_id,
         seq["last_sequence"],_COORDINATOR,json.dumps({
             "background_epoch": epoch["background_epoch"],
             "previous_stage": old_stage,"stage": new_stage,"reason": reason,
         })),
    )
    cursor.execute(
        "INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
        (uuid4(),event_id),
    )


def _change_stage(cursor, epoch: dict, target: str, *, reason: str | None = None) -> None:
    current = epoch["preparation_stage"]
    cursor.execute(
        """UPDATE background_preparation_epochs
           SET preparation_stage=%s,preparation_updated_at=clock_timestamp()
           WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
             AND status='PENDING' AND preparation_stage=%s
           RETURNING background_epoch""",
        (target, epoch["user_id"], epoch["instance_id"],
         epoch["background_epoch"], current),
    )
    if cursor.fetchone() is None:
        raise StaleBackgroundPreparation("epoch stage changed")
    _event(cursor, epoch, current, target, reason=reason)
    epoch["preparation_stage"] = target


def _job_status(cursor, conversation_id: UUID, expected_generation: int,
                start_sequence: int, *, summary: bool) -> tuple[UUID | None, str | None]:
    table = "conversation_summary_jobs" if summary else "cognitive_jobs"
    cursor.execute(
        f"""SELECT j.operation_id,o.status
            FROM {table} j
            LEFT JOIN operations o ON o.operation_id=j.operation_id
            WHERE j.conversation_id=%s AND j.start_sequence=%s
              AND j.expected_generation=%s
            ORDER BY j.admission_attempt DESC LIMIT 1""",
        (conversation_id,start_sequence,expected_generation),
    )
    row = cursor.fetchone()
    return ((row["operation_id"],row["status"]) if row else (None,None))


def advance_background_preparation(
    connection: Connection, actor: TrustedPlatformActor,
    instance_id: UUID, background_epoch: int,
) -> PreparationProgress:
    """One bounded, idempotent, SQL-first preparation step.

    Uses a short transaction to check frozen input, current generation and
    persisted watermarks. Schedules at most one *existing P0* operation after
    the transaction. No inference or tool call occurs under a DB lock.
    Concurrent ticks may choose the same work; P2 stage/enqueue is idempotent.
    """
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor or actor.source_id != _COORDINATOR:
        raise PermissionDenied("preparation requires the trusted coordinator")
    if background_epoch < 1:
        raise ValueError("invalid background epoch")

    to_schedule = None
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            # The shared selection lock keeps the lock order consistent with
            # lifecycle transitions. Never take epoch before selection.
            cur.execute(
                """SELECT instance_id,generation,mode,background_epoch
                   FROM persona_selection WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected = cur.fetchone()
            if (selected is None or selected["instance_id"] != instance_id
                or selected["mode"] != "BACKGROUND"
                or selected["background_epoch"] != background_epoch):
                raise StaleBackgroundPreparation("persona not in selected Background epoch")
            cur.execute(
                """SELECT * FROM background_preparation_epochs
                   WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                   FOR UPDATE""",
                (actor.user_id,instance_id,background_epoch),
            )
            epoch=cur.fetchone()
            if (epoch is None or epoch["status"] != "PENDING"
                or epoch["selection_generation"] != selected["generation"]):
                raise StaleBackgroundPreparation("preparation epoch revoked")
            stage=epoch["preparation_stage"]
            if stage == "BLOCKED":
                return PreparationProgress(stage,"BLOCKED")
            if stage == "CRITICAL_PENDING":
                return PreparationProgress(stage,"AWAITING_OWNER_COMMITS")

            cur.execute(
                """SELECT b.conversation_id,b.target_sequence,
                          c.summary_watermark,c.cognitive_watermark
                   FROM background_preparation_conversations b
                   JOIN conversations c
                     ON c.conversation_id=b.conversation_id
                    AND c.user_id=b.user_id AND c.instance_id=b.instance_id
                   WHERE b.user_id=%s AND b.instance_id=%s
                     AND b.background_epoch=%s
                   ORDER BY b.conversation_id""",
                (actor.user_id,instance_id,background_epoch),
            )
            rows=cur.fetchall()
            if len(rows) != epoch["captured_conversation_count"]:
                _change_stage(cur,epoch,"BLOCKED",reason="incomplete frozen conversation snapshot")
                return PreparationProgress("BLOCKED","REQUIRES_OPERATOR")
            for step in ("SUMMARIZE","EVALUATE"):
                if stage != step:
                    continue
                summary = step == "SUMMARIZE"
                watermark_key = "summary_watermark" if summary else "cognitive_watermark"
                pending = next(
                    (r for r in rows if r[watermark_key]<r["target_sequence"]),
                    None,
                )
                if pending is not None:
                    operation_id,status=_job_status(
                        cur,pending["conversation_id"],selected["generation"],
                        pending[watermark_key]+1,summary=summary,
                    )
                    if status in _TERMINAL or status == "COMPLETED":
                        # READ COMMITTED: the worker may commit its watermark
                        # between the snapshot SELECT and this job-status read.
                        # Recheck after seeing a terminal state before marking
                        # a successfully processed epoch permanently BLOCKED.
                        cur.execute(
                            """SELECT summary_watermark,cognitive_watermark
                               FROM conversations
                               WHERE conversation_id=%s AND user_id=%s
                                 AND instance_id=%s""",
                            (pending["conversation_id"],actor.user_id,instance_id),
                        )
                        fresh=cur.fetchone()
                        if fresh is not None and fresh[watermark_key] > pending[watermark_key]:
                            return PreparationProgress(
                                step,"RECHECK_WATERMARK",
                                pending["conversation_id"],operation_id,
                            )
                        _change_stage(cur,epoch,"BLOCKED",
                                      reason=f"{step}: {status} operation")
                        return PreparationProgress("BLOCKED","REQUIRES_OPERATOR",
                                                   pending["conversation_id"],operation_id)
                    if status in _IN_PROGRESS:
                        return PreparationProgress(step,"WAITING_OPERATION",
                                                   pending["conversation_id"],operation_id)
                    # New work, or staged-before-enqueue crash: the owning
                    # summary/cognitive scheduler repairs the exact operation.
                    to_schedule=(summary,pending["conversation_id"],
                                 selected["generation"])
                    break
                next_stage = "EVALUATE" if summary else "CRITICAL_PENDING"
                _change_stage(cur,epoch,next_stage)
                stage=next_stage
                if stage == "CRITICAL_PENDING":
                    return PreparationProgress(stage,"AWAITING_OWNER_COMMITS")
            if to_schedule is None:
                # Only reachable if a stage was advanced but not yet evaluated.
                return PreparationProgress(stage,"STAGE_ADVANCED")

    summary,conversation_id,generation=to_schedule
    try:
        if summary:
            result=schedule_summary(
                connection,actor,conversation_id,instance_id,generation,
                retain_recent=0,max_turns=8,
            )
        else:
            result=schedule_cognition(
                connection,actor,conversation_id,instance_id,generation,
                retain_recent=0,max_turns=8,min_closed_turns=1,
            )
    except (SummaryNotReady,CognitiveNotReady):
        return PreparationProgress("SUMMARIZE" if summary else "EVALUATE",
                                   "WAITING_RESPONSE",conversation_id)
    except (StaleSummary,StaleCognition,StaleSelection) as exc:
        # No readiness is granted; a superseded epoch is not retried here.
        raise StaleBackgroundPreparation(str(exc)) from exc
    return PreparationProgress(
        "SUMMARIZE" if summary else "EVALUATE",
        "ENQUEUED" if result is not None else "WAITING_OPERATION",
        conversation_id,result.operation_id if result is not None else None,
    )


def run_preparation_scan(connection: Connection, *, limit: int = 32
                         ) -> tuple[PreparationProgress, ...]:
    """Bounded coordinator polling; not a second worker/operation scheduler."""
    require_idle(connection)
    if not 1 <= limit <= 128:
        raise ValueError("invalid scan limit")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT e.user_id,e.instance_id,e.background_epoch
               FROM background_preparation_epochs e
               JOIN persona_selection s ON s.user_id=e.user_id
                AND s.instance_id=e.instance_id
                AND s.generation=e.selection_generation
                AND s.background_epoch=e.background_epoch
                AND s.mode='BACKGROUND'
               WHERE e.status='PENDING'
                 AND e.preparation_stage IN ('SUMMARIZE','EVALUATE')
               ORDER BY e.preparation_updated_at,e.user_id,e.background_epoch
               LIMIT %s""",
            (limit,),
        )
        epochs=tuple(cur.fetchall())
    outcomes=[]
    for epoch in epochs:
        try:
            progress=advance_background_preparation(
                connection,
                TrustedPlatformActor(user_id=epoch["user_id"],source_id=_COORDINATOR),
                epoch["instance_id"],epoch["background_epoch"],
            )
        except StaleBackgroundPreparation:
            # Return/switch raced with discovery; the next scan ignores it.
            continue
        outcomes.append(progress)
        if progress.stage in ("SUMMARIZE", "EVALUATE"):
            # A permanently waiting earlier epoch must not starve the next
            # page of otherwise eligible instances. This is only a polling
            # cursor (not an authorization or readiness transition).
            with connection.transaction():
                connection.execute(
                    """UPDATE background_preparation_epochs
                       SET preparation_updated_at=clock_timestamp()
                       WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                         AND status='PENDING'
                         AND preparation_stage IN ('SUMMARIZE','EVALUATE')""",
                    (epoch["user_id"],epoch["instance_id"],epoch["background_epoch"]),
                )
    return tuple(outcomes)
