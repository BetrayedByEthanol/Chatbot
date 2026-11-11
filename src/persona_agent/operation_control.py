"""P0-04 fenced operation effects, cancellation, suspension and recovery."""

from datetime import timedelta
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .operation_contracts import (
    EffectClass, InvalidOperation, OperationLease, Policy, StaleOperation,
    Status, event_for, lock_operation, release_resources,
    require_claim, require_current_authority, require_idle,
)
from .runtime import IdempotencyConflict, PermissionDenied, _fingerprint


class CancelOperationCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    operation_id: UUID
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1,max_length=128)


def _update_state(cursor, row: dict, status: str, *, error: str | None = None,
                  result_ref: str | None = None, eligible_delay: int = 0) -> dict:
    cursor.execute(
        """
        UPDATE operations SET status=%s,worker_id=NULL,lease_until=NULL,
            claimed_at=NULL,version=version+1,updated_at=clock_timestamp(),
            eligible_at=clock_timestamp()+(%s*interval '1 second'),
            last_error=%s,result_ref=coalesce(%s,result_ref)
        WHERE operation_id=%s RETURNING *
        """,
        (status,eligible_delay,error,result_ref,row["operation_id"]),
    )
    return cursor.fetchone()


def _unknown(
    cursor, row: dict, reason: str, *, lease: OperationLease | None = None,
) -> dict:
    """Unknown external effects retain reservations until reconciled.

    Recovery owns its already-expired attempt and may quarantine it directly.
    A worker reporting failure must instead pass the still-valid lease fence.
    """
    if lease is None:
        worker_fence=""
        params=(reason,row["operation_id"])
    else:
        worker_fence="""
          AND status='RUNNING' AND effect_status='DISPATCHED'
          AND worker_id=%s AND claim_generation=%s
          AND selection_generation=%s
          AND lease_until > clock_timestamp()
          AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
          AND (deadline IS NULL OR deadline > clock_timestamp())
        """
        params=(reason,row["operation_id"],lease.worker_id,
                lease.claim_generation,lease.selection_generation)
    cursor.execute(
        """
        UPDATE operations SET status='OUTCOME_UNKNOWN',
            effect_status='UNKNOWN',worker_id=NULL,lease_until=NULL,
            claimed_at=NULL,last_error=%s,version=version+1,
            updated_at=clock_timestamp()
        WHERE operation_id=%s
        """+worker_fence+"""
        RETURNING *
        """,
        params,
    )
    updated=cursor.fetchone()
    if updated is None:
        raise StaleOperation("external failure fence expired before commit")
    event_id=event_for(cursor,updated,"operation.outcome_unknown.v1",{"reason":reason})
    return updated,event_id


def heartbeat_operation(
    connection: Connection, lease: OperationLease, *, extend_seconds: int = 30,
) -> OperationLease:
    """Lease refresh; no event on every heartbeat to avoid journal spam."""
    require_idle(connection)
    if not 1 <= extend_seconds <= 3600:
        raise ValueError("invalid heartbeat extension")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease)
            require_current_authority(cursor,row)
            # The selection/input-state locks above may block past the
            # original lease expiry without changing persona authority.
            # A second post-lock check and a guarded UPDATE prevent a
            # stale heartbeat from reviving the expired attempt.
            require_claim(cursor,lease)
            cursor.execute(
                """
                UPDATE operations SET
                    lease_until=LEAST(
                        clock_timestamp()+(%s*interval '1 second'),
                        claimed_at+(timeout_seconds*interval '1 second'),
                        COALESCE(deadline,'infinity'::timestamptz)
                    ),updated_at=clock_timestamp()
                WHERE operation_id=%s
                  AND status='RUNNING'
                  AND worker_id=%s AND claim_generation=%s
                  AND selection_generation=%s
                  AND lease_until > clock_timestamp()
                  AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                RETURNING lease_until
                """,
                (extend_seconds,row["operation_id"],lease.worker_id,
                 lease.claim_generation,lease.selection_generation),
            )
            updated=cursor.fetchone()
            if updated is None:
                raise StaleOperation("heartbeat lease expired before extension")
            return lease.model_copy(update={"lease_until":updated["lease_until"]})


def checkpoint_operation(
    connection: Connection, lease: OperationLease, *, checkpoint_ref: str,
) -> None:
    """Durable checkpoint; rejected after epoch change or persona switch."""
    require_idle(connection)
    if not checkpoint_ref or len(checkpoint_ref)>1000:
        raise ValueError("checkpoint_ref is required (up to 1000 characters)")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease)
            require_current_authority(cursor,row)
            # The authority lock can block beyond the original lease,
            # without changing the selected persona or generation.
            # Recheck after the lock and fence the final database write.
            require_claim(cursor,lease)
            cursor.execute(
                """
                UPDATE operations SET checkpoint_ref=%s,version=version+1,
                    updated_at=clock_timestamp()
                WHERE operation_id=%s
                  AND status='RUNNING'
                  AND worker_id=%s AND claim_generation=%s
                  AND selection_generation=%s
                  AND lease_until > clock_timestamp()
                  AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                RETURNING *
                """,
                (checkpoint_ref,row["operation_id"],lease.worker_id,
                 lease.claim_generation,lease.selection_generation),
            )
            updated=cursor.fetchone()
            if updated is None:
                raise StaleOperation("checkpoint fence expired before commit")
            event_for(cursor,updated,"operation.checkpointed.v1")


def mark_external_dispatched(
    connection: Connection, lease: OperationLease,
) -> None:
    """Durably record external dispatch intent; caller revalidates immediately.

    Execution happens OUTSIDE this transaction. A crash after dispatch is an
    unknown outcome, not proof that the provider action failed.
    """
    require_idle(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease)
            require_current_authority(cursor,row)
            if row["effect_class"] != EffectClass.EXTERNAL.value:
                raise InvalidOperation("only external-effect operations can be dispatched")
            if row["effect_status"] != "PREPARED":
                raise InvalidOperation("external effect already dispatched; reconcile instead")
            # The selection/input-state locks above may have blocked until
            # after expiry without changing the selected persona or mode.
            require_claim(cursor,lease)
            cursor.execute(
                """
                UPDATE operations SET effect_status='DISPATCHED',
                    version=version+1,updated_at=clock_timestamp()
                WHERE operation_id=%s
                  AND status='RUNNING'
                  AND effect_class='EXTERNAL' AND effect_status='PREPARED'
                  AND worker_id=%s AND claim_generation=%s
                  AND selection_generation=%s
                  AND lease_until > clock_timestamp()
                  AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                RETURNING *
                """,
                (row["operation_id"],lease.worker_id,
                 lease.claim_generation,lease.selection_generation),
            )
            updated=cursor.fetchone()
            if updated is None:
                raise StaleOperation("external dispatch fence expired before commit")
            event_for(cursor,updated,"operation.effect_dispatched.v1")


def complete_operation(
    connection: Connection, lease: OperationLease, *, result_ref: str,
) -> None:
    require_idle(connection)
    if not result_ref or len(result_ref)>1000:
        raise ValueError("result_ref is required (up to 1000 characters)")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease)
            require_current_authority(cursor,row)
            if (row["effect_class"]=="EXTERNAL"
                and row["effect_status"]!="DISPATCHED"):
                raise InvalidOperation("external effects require a prior dispatch record")
            release_resources(cursor,row)
            # Selection and pool locks can outlive a lease. Validate after
            # acquiring them, then enforce time fences again in the UPDATE.
            # Rejection rolls back released reservations and all other writes.
            require_claim(cursor,lease)
            cursor.execute(
                """
                UPDATE operations SET status='COMPLETED',
                    worker_id=NULL,lease_until=NULL,claimed_at=NULL,
                    effect_status='CONFIRMED',result_ref=%s,
                    version=version+1,updated_at=clock_timestamp()
                WHERE operation_id=%s
                  AND status='RUNNING'
                  AND worker_id=%s AND claim_generation=%s
                  AND selection_generation=%s
                  AND lease_until > clock_timestamp()
                  AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                RETURNING *
                """,
                (result_ref,row["operation_id"],lease.worker_id,
                 lease.claim_generation,lease.selection_generation),
            )
            updated=cursor.fetchone()
            if updated is None:
                raise StaleOperation("completion fence expired before commit")
            event_for(cursor,updated,"operation.completed.v1",{
                "result_ref":result_ref,
            })


def fail_operation(
    connection: Connection, lease: OperationLease, *,
    reason: str, delay_seconds: int = 1,
) -> Status:
    require_idle(connection)
    if not 0 <= delay_seconds <= 3600:
        raise ValueError("retry delay must be 0..3600 seconds")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease)
            require_current_authority(cursor,row)
            if row["effect_status"]=="DISPATCHED":
                # The authority locks may have blocked beyond the original
                # lease or hard deadline. Worker-driven quarantine is fenced;
                # recovery retains separate authority over expired attempts.
                require_claim(cursor,lease)
                _unknown(cursor,row,reason[:1000],lease=lease)
                return Status.OUTCOME_UNKNOWN
            status=("FAILED" if row["attempt_count"]>=row["max_attempts"]
                    else "RETRY_WAIT")
            release_resources(cursor,row)
            # Pool locks may have blocked past lease, timeout or deadline.
            # Rejecting the worker rolls back the resource release entirely;
            # recovery then decides whether the work is retryable or failed.
            require_claim(cursor,lease)
            cursor.execute(
                """
                UPDATE operations SET status=%s,worker_id=NULL,
                    lease_until=NULL,claimed_at=NULL,
                    version=version+1,updated_at=clock_timestamp(),
                    eligible_at=clock_timestamp()+(%s*interval '1 second'),
                    last_error=%s
                WHERE operation_id=%s
                  AND status='RUNNING'
                  AND worker_id=%s AND claim_generation=%s
                  AND selection_generation=%s
                  AND lease_until > clock_timestamp()
                  AND claimed_at+(timeout_seconds*interval '1 second') > clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                RETURNING *
                """,
                (status,delay_seconds,reason[:1000],row["operation_id"],
                 lease.worker_id,lease.claim_generation,lease.selection_generation),
            )
            updated=cursor.fetchone()
            if updated is None:
                raise StaleOperation("failure fence expired before commit")
            event_for(cursor,updated,"operation.failed.v1" if status=="FAILED"
                      else "operation.retry_wait.v1",{"reason":reason[:1000]})
            return Status(status)


def cancel_operation(
    connection: Connection, actor: TrustedActor, command: CancelOperationCommand,
) -> Status:
    """User-scoped cancellation works even if persona is now INACTIVE."""
    require_idle(connection)
    if type(actor) is not TrustedActor:
        raise PermissionDenied("cancellation requires authenticated primary user")
    fingerprint=_fingerprint(command)
    receipt_id=uuid4()
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT 1 FROM operations WHERE operation_id=%s AND user_id=%s
                """,
                (command.operation_id,actor.user_id),
            )
            if cursor.fetchone() is None:
                raise PermissionDenied("operation not owned by authenticated user")
            cursor.execute(
                """
                SELECT instance_id FROM operations WHERE operation_id=%s
                """,(command.operation_id,),
            )
            instance_id=cursor.fetchone()["instance_id"]
            cursor.execute(
                """
                INSERT INTO command_receipts(
                    receipt_id,user_id,instance_id,command_type,
                    idempotency_key,request_fingerprint
                ) VALUES (%s,%s,%s,'operation.cancel.v1',%s,%s)
                ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                DO NOTHING RETURNING receipt_id
                """,
                (receipt_id,actor.user_id,instance_id,
                 command.idempotency_key,fingerprint),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    SELECT request_fingerprint,result_json FROM command_receipts
                    WHERE user_id=%s AND instance_id=%s
                      AND command_type='operation.cancel.v1' AND idempotency_key=%s
                    """,
                    (actor.user_id,instance_id,command.idempotency_key),
                )
                receipt=cursor.fetchone()
                if receipt is None or receipt["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("cancellation key reused with new content")
                return Status(receipt["result_json"]["status"])
            row=lock_operation(cursor,command.operation_id)
            if row["version"]!=command.expected_version:
                raise StaleOperation("operation version changed")
            if row["status"] in ("COMPLETED","FAILED","CANCELLED","OUTCOME_UNKNOWN"):
                raise InvalidOperation("terminal operation cannot be cancelled")
            if row["effect_status"]=="DISPATCHED":
                _,event_id=_unknown(cursor,row,"cancel requested after external dispatch")
                updated_status=Status.OUTCOME_UNKNOWN
            elif row["status"] in ("RUNNING","CANCEL_REQUESTED","SUSPEND_REQUESTED"):
                cursor.execute(
                    """
                    UPDATE operations SET status='CANCEL_REQUESTED',
                        version=version+1,updated_at=clock_timestamp()
                    WHERE operation_id=%s RETURNING *
                    """,(row["operation_id"],),
                )
                updated=cursor.fetchone()
                event_id=event_for(cursor,updated,"operation.cancel_requested.v1")
                updated_status=Status.CANCEL_REQUESTED
            else:
                release_resources(cursor,row)
                updated=_update_state(cursor,row,"CANCELLED")
                event_id=event_for(cursor,updated,"operation.cancelled.v1")
                updated_status=Status.CANCELLED
            cursor.execute(
                """
                UPDATE command_receipts SET result_json=%s::jsonb,event_id=%s
                WHERE receipt_id=%s
                """,('{"status":"'+updated_status.value+'"}',event_id,receipt_id),
            )
            return updated_status


def acknowledge_cancel(connection: Connection, lease: OperationLease) -> None:
    """Stale result cannot commit, but a currently claimed cancelled task may clean up."""
    require_idle(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=require_claim(cursor,lease,statuses=("CANCEL_REQUESTED",))
            release_resources(cursor,row)
            updated=_update_state(cursor,row,"CANCELLED")
            event_for(cursor,updated,"operation.cancelled.v1")


def resume_suspended(
    connection: Connection, actor: TrustedActor, *, operation_id: UUID,
) -> None:
    """Explicit re-admission, without inheriting stale prior checkpoint authority."""
    require_idle(connection)
    if type(actor) is not TrustedActor:
        raise PermissionDenied("resume requires authenticated primary user")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=lock_operation(cursor,operation_id)
            if row["user_id"]!=actor.user_id:
                raise PermissionDenied("operation outside authenticated user scope")
            if row["status"]!="SUSPENDED" or row["effect_status"]!="PREPARED":
                raise InvalidOperation("only safely suspended, undispatched work can resume")
            if row["input_versions"]:
                raise InvalidOperation("owner must revalidate input versions before resume")
            cursor.execute(
                """
                SELECT instance_id,generation FROM persona_selection
                WHERE user_id=%s FOR SHARE
                """,(actor.user_id,),
            )
            selected=cursor.fetchone()
            if selected is None or selected["instance_id"] != row["instance_id"]:
                raise StaleOperation("persona must be selected for re-admission")
            cursor.execute(
                """
                UPDATE operations SET status='QUEUED',
                    selection_generation=%s,background_epoch=NULL,
                    eligible_at=clock_timestamp(),version=version+1,
                    updated_at=clock_timestamp()
                WHERE operation_id=%s RETURNING *
                """,(selected["generation"],operation_id),
            )
            event_for(cursor,cursor.fetchone(),"operation.resumed.v1",
                      {"selection_generation":selected["generation"]})


def reconcile_unknown(
    connection: Connection, actor: TrustedPlatformActor,
    *, operation_id: UUID, confirmed: bool, evidence_ref: str,
) -> Status:
    """Operator-confirmed provider reconciliation; not automatic lease recovery."""
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor:
        raise PermissionDenied("reconciliation requires trusted platform authority")
    if not evidence_ref or len(evidence_ref)>1000:
        raise ValueError("provider reconciliation evidence is required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            row=lock_operation(cursor,operation_id)
            if row["user_id"]!=actor.user_id:
                raise PermissionDenied("cross-user reconciliation forbidden")
            if row["status"]!="OUTCOME_UNKNOWN":
                raise InvalidOperation("operation does not have unknown outcome")
            release_resources(cursor,row)
            status="COMPLETED" if confirmed else "FAILED"
            cursor.execute(
                """
                UPDATE operations SET status=%s,effect_status=%s,
                    result_ref=%s,version=version+1,updated_at=clock_timestamp()
                WHERE operation_id=%s RETURNING *
                """,
                (
                    status,"CONFIRMED" if confirmed else "FAILED",
                    evidence_ref,operation_id,
                ),
            )
            event_for(cursor,cursor.fetchone(),"operation.reconciled.v1",{
                "status":status,"evidence_ref":evidence_ref,
            })
            return Status(status)


def reap_stale_operations(connection: Connection, *, limit: int=100) -> int:
    """Recover expired/cancelled/invalidated attempts without assuming side effects failed."""
    require_idle(connection)
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    processed=0
    # Candidate discovery runs in autocommit: do not retain row/pool locks.
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT o.operation_id FROM operations o
            LEFT JOIN persona_selection s ON s.user_id=o.user_id
            LEFT JOIN persona_instances i ON i.user_id=o.user_id AND i.id=o.instance_id
            WHERE o.status IN ('RUNNING','CANCEL_REQUESTED','SUSPEND_REQUESTED')
              AND (
                o.lease_until <= clock_timestamp()
                OR s.instance_id IS DISTINCT FROM o.instance_id
                OR s.generation IS DISTINCT FROM o.selection_generation
                OR (o.claimed_at+(o.timeout_seconds*interval '1 second')
                    <= clock_timestamp())
                OR (o.deadline IS NOT NULL AND o.deadline <= clock_timestamp())
                OR (o.input_versions ? 'persona_instance' AND
                    (o.input_versions->>'persona_instance')::bigint
                       IS DISTINCT FROM i.version)
                OR (o.execution_policy <> 'FOREGROUND_ASYNC'
                    AND (s.mode IS DISTINCT FROM 'BACKGROUND'
                         OR s.background_epoch IS DISTINCT FROM o.background_epoch))
              )
            ORDER BY o.lease_until,o.operation_id LIMIT %s
            """,(limit,),
        )
        candidates=[r["operation_id"] for r in cursor.fetchall()]
    for op_id in candidates:
        # One short transaction per operation. Its resource release, state,
        # event and outbox commit before inspecting the next operation.
        with connection.transaction():
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT * FROM operations
                    WHERE operation_id=%s AND status IN (
                       'RUNNING','CANCEL_REQUESTED','SUSPEND_REQUESTED')
                    FOR UPDATE SKIP LOCKED
                    """,(op_id,),
                )
                row=cursor.fetchone()
                if row is None:
                    continue
                cursor.execute(
                    """
                    SELECT instance_id,generation,mode,background_epoch
                    FROM persona_selection WHERE user_id=%s FOR SHARE
                    """,(row["user_id"],),
                )
                selected=cursor.fetchone()
                cursor.execute("SELECT clock_timestamp() AS server_now")
                now=cursor.fetchone()["server_now"]
                revoked=(
                    selected is None or selected["instance_id"]!=row["instance_id"]
                    or selected["generation"]!=row["selection_generation"]
                )
                epoch_invalid=(
                    row["execution_policy"]!="FOREGROUND_ASYNC"
                    and (selected is None or selected["mode"]!="BACKGROUND"
                         or selected["background_epoch"]!=row["background_epoch"])
                )
                expired=(
                    row["lease_until"]<=now
                    or row["claimed_at"] is not None
                    and row["claimed_at"]+timedelta(
                        seconds=row["timeout_seconds"])<=now
                )
                deadline_passed=row["deadline"] is not None and row["deadline"]<=now
                input_invalid=False
                if "persona_instance" in row["input_versions"]:
                    cursor.execute(
                        """
                        SELECT version FROM persona_instances
                        WHERE user_id=%s AND id=%s FOR SHARE
                        """,(row["user_id"],row["instance_id"]),
                    )
                    input_version=cursor.fetchone()
                    input_invalid=(
                        input_version is None or
                        input_version["version"]!=row["input_versions"]["persona_instance"]
                    )
                if (not revoked and not epoch_invalid and not expired
                    and not deadline_passed and not input_invalid
                    and row["status"]=="RUNNING"):
                    continue
                if row["effect_status"]=="DISPATCHED":
                    _unknown(cursor,row,"worker interrupted after external dispatch")
                else:
                    if row["status"]=="CANCEL_REQUESTED":
                        status="CANCELLED"
                    elif row["status"]=="SUSPEND_REQUESTED":
                        status="SUSPENDED"
                    elif (revoked or epoch_invalid):
                        status=("CANCELLED" if row["execution_policy"]=="BACKGROUND_EPHEMERAL"
                                else "SUSPENDED")
                    elif deadline_passed or row["attempt_count"]>=row["max_attempts"]:
                        status="FAILED"
                    elif input_invalid:
                        status="BLOCKED"
                    else:
                        status="RETRY_WAIT"
                    release_resources(cursor,row)
                    updated=_update_state(
                        cursor,row,status,
                        error="worker lease, selection or background epoch invalidated",
                        eligible_delay=min(3600,2**min(row["attempt_count"],10))
                        if status=="RETRY_WAIT" else 0,
                    )
                    event_for(cursor,updated,"operation.recovered.v1",{"status":status})
                processed+=1

    # Pending work is discovered separately after processing active claims.
    if processed < limit:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT o.operation_id FROM operations o
                LEFT JOIN persona_selection s ON s.user_id=o.user_id
                LEFT JOIN persona_instances i ON i.user_id=o.user_id AND i.id=o.instance_id
                WHERE o.status IN ('QUEUED','RETRY_WAIT')
                  AND (
                    (o.deadline IS NOT NULL AND o.deadline <= clock_timestamp())
                    OR s.instance_id IS DISTINCT FROM o.instance_id
                    OR s.generation IS DISTINCT FROM o.selection_generation
                    OR (o.input_versions ? 'persona_instance' AND
                        (o.input_versions->>'persona_instance')::bigint
                           IS DISTINCT FROM i.version)
                    OR (o.execution_policy = 'BACKGROUND_EPHEMERAL'
                        AND (o.background_epoch IS NULL OR NOT (
                            (s.mode = 'BACKGROUND'
                             AND s.background_epoch = o.background_epoch)
                            OR (s.mode = 'FOREGROUND'
                                AND s.background_epoch + 1 = o.background_epoch)
                        )))
                    OR (o.execution_policy = 'BACKGROUND_RESUMABLE'
                        AND o.background_epoch IS NOT NULL
                        AND (s.mode IS DISTINCT FROM 'BACKGROUND'
                             OR s.background_epoch IS DISTINCT FROM o.background_epoch))
                  )
                ORDER BY o.created_at,o.operation_id LIMIT %s
                """,(limit-processed,),
            )
            pending=[r["operation_id"] for r in cursor.fetchall()]
        for op_id in pending:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        """
                        SELECT * FROM operations
                        WHERE operation_id=%s AND status IN ('QUEUED','RETRY_WAIT')
                        FOR UPDATE SKIP LOCKED
                        """,(op_id,),
                    )
                    row=cursor.fetchone()
                    if row is None:
                        continue
                    cursor.execute(
                        """
                        SELECT instance_id,generation,mode,background_epoch
                        FROM persona_selection WHERE user_id=%s FOR SHARE
                        """,(row["user_id"],),
                    )
                    selected=cursor.fetchone()
                    cursor.execute("SELECT clock_timestamp() AS server_now")
                    now=cursor.fetchone()["server_now"]
                    revoked=(
                        selected is None or selected["instance_id"]!=row["instance_id"]
                        or selected["generation"]!=row["selection_generation"]
                    )
                    if row["execution_policy"]=="BACKGROUND_EPHEMERAL":
                        epoch_invalid=(
                            row["background_epoch"] is None
                            or selected is None or not (
                                (selected["mode"]=="BACKGROUND"
                                 and selected["background_epoch"]==row["background_epoch"])
                                or (selected["mode"]=="FOREGROUND"
                                    and selected["background_epoch"]+1
                                        ==row["background_epoch"])
                            )
                        )
                    else:
                        epoch_invalid=(
                            row["execution_policy"]=="BACKGROUND_RESUMABLE"
                            and row["background_epoch"] is not None
                            and (selected is None or selected["mode"]!="BACKGROUND"
                                 or selected["background_epoch"]!=row["background_epoch"])
                        )
                    deadline_passed=row["deadline"] is not None and row["deadline"]<=now
                    input_invalid=False
                    if "persona_instance" in row["input_versions"]:
                        cursor.execute(
                            """
                            SELECT version FROM persona_instances
                            WHERE user_id=%s AND id=%s FOR SHARE
                            """,(row["user_id"],row["instance_id"]),
                        )
                        input_version=cursor.fetchone()
                        input_invalid=(
                            input_version is None or
                            input_version["version"]!=row["input_versions"]["persona_instance"]
                        )
                    if not (revoked or epoch_invalid or deadline_passed or input_invalid):
                        continue
                    status=(
                        "FAILED" if deadline_passed else
                        "BLOCKED" if input_invalid else
                        "CANCELLED" if row["execution_policy"]=="BACKGROUND_EPHEMERAL"
                        else "SUSPENDED"
                    )
                    release_resources(cursor,row)
                    updated=_update_state(
                        cursor,row,status,error="queued work ineligible after authority, epoch or deadline change",
                    )
                    event_for(cursor,updated,"operation.recovered.v1",{"status":status})
                    processed+=1
    return processed
