"""P0-04 operation admission: scoped enqueue, capability/dependency checks, fenced claim."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .summary_contracts import SUMMARY_OPERATION, SUMMARY_CAPABILITY, SUMMARY_RESOURCE
from .operation_contracts import (
    EffectClass, EnqueueOperation, EnqueueResult, InvalidOperation, Origin,
    OperationLease, Policy, Priority, ResourceUnavailable, Status,
    event_for, require_idle,
)
from .runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
    _fingerprint,
)


def configure_resource_pool(
    connection: Connection,
    resource_key: str,
    *,
    capacity_units: int,
    p0_reserved_units: int = 0,
) -> None:
    """Trusted deployment configuration; never expose as persona/tool command."""
    require_idle(connection)
    if not 1 <= len(resource_key) <= 128:
        raise ValueError("invalid resource key")
    if not 0 <= p0_reserved_units <= capacity_units or capacity_units <= 0:
        raise ValueError("invalid capacity or P0 reserve")
    with connection.transaction():
        cursor = connection.execute(
            """
            INSERT INTO execution_resource_pools (
                resource_key,capacity_units,p0_reserved_units
            ) VALUES (%s,%s,%s)
            ON CONFLICT(resource_key) DO UPDATE
            SET capacity_units=excluded.capacity_units,
                p0_reserved_units=excluded.p0_reserved_units
            WHERE execution_resource_pools.active_units <= excluded.capacity_units
              AND execution_resource_pools.active_non_p0_units
                    <= excluded.capacity_units - excluded.p0_reserved_units
            RETURNING resource_key
            """,
            (resource_key,capacity_units,p0_reserved_units),
        )
        if cursor.fetchone() is None:
            raise ResourceUnavailable(
                "pool reconfiguration would violate active total or non-P0 reservations"
            )


def enqueue_operation(
    connection: Connection,
    actor: TrustedActor | TrustedPlatformActor,
    command: EnqueueOperation,
) -> EnqueueResult:
    """Idempotent admission to durable queue; actual worker eligibility is rechecked."""
    require_idle(connection)
    platform = type(actor) is TrustedPlatformActor
    if type(actor) is not TrustedActor and not platform:
        raise PermissionDenied("enqueue requires a trusted user or platform actor")
    # P2-03 grants the internal automation worker ONLY non-effecting summary
    # admission. It is not a generic platform permission to enqueue tools,
    # goals, external effects or arbitrary cognitive operations.
    is_summary_admission=(
        command.operation_type == SUMMARY_OPERATION
        and command.origin in (Origin.USER,Origin.COGNITIVE)
        and command.effect_class is EffectClass.NONE
        and command.execution_policy is Policy.FOREGROUND_ASYNC
        and command.priority_class is Priority.P1
        and command.resources == {SUMMARY_RESOURCE: 1}
        and command.capabilities == (SUMMARY_CAPABILITY,)
        and not command.dependencies
        and command.task_id is None
        and command.eligible_at is None
        and command.deadline is None
        and command.timeout_seconds == 300
        and command.max_attempts == 3
        and not command.input_versions
    )
    if platform and not is_summary_admission:
        raise PermissionDenied("platform admission is restricted to memory summaries")
    # A caller can repair a job staged by the OTHER authorized producer,
    # but may not create fresh operations with that producer's origin.
    # Validate the immutable P2-03 staged job before a cross-origin enqueue.
    cross_origin=(
        (platform and command.origin is Origin.USER)
        or (not platform and command.origin is Origin.COGNITIVE)
    )
    cross_origin_denied=(
        "cross-origin admission requires a staged summary" if platform else
        "agent-originated operation requires a platform actor"
    )
    if cross_origin and (
        not is_summary_admission
        or command.idempotency_key!=f"summary:{command.operation_id}"
    ):
        raise PermissionDenied(cross_origin_denied)
    fingerprint = _fingerprint(command)
    receipt_id=uuid4()

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            if cross_origin:
                cur.execute(
                    """SELECT conversation_id FROM conversation_summary_jobs
                       WHERE operation_id=%s AND user_id=%s AND instance_id=%s
                         AND expected_generation=%s AND admission_origin=%s""",
                    (command.operation_id,actor.user_id,command.instance_id,
                     command.expected_generation,command.origin.value),
                )
                staged=cur.fetchone()
                if (staged is None or command.concurrency_group
                    !=f"summary:{staged['conversation_id']}"):
                    raise PermissionDenied(cross_origin_denied)
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("operation instance outside authenticated scope")
            cur.execute(
                """
                INSERT INTO command_receipts (
                    receipt_id,user_id,instance_id,command_type,
                    idempotency_key,request_fingerprint
                ) VALUES (%s,%s,%s,'operation.enqueue.v1',%s,%s)
                ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                DO NOTHING RETURNING receipt_id
                """,
                (
                    receipt_id,actor.user_id,command.instance_id,
                    command.idempotency_key,fingerprint,
                ),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """
                    SELECT request_fingerprint,result_json
                    FROM command_receipts
                    WHERE user_id=%s AND instance_id=%s
                      AND command_type='operation.enqueue.v1'
                      AND idempotency_key=%s
                    """,
                    (actor.user_id,command.instance_id,command.idempotency_key),
                )
                prior=cur.fetchone()
                if prior is None or prior["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("same enqueue key with different content")
                if prior["result_json"] is None:
                    raise InvalidOperation("committed enqueue is missing durable result")
                return EnqueueResult.model_validate(prior["result_json"]).model_copy(
                    update={"replayed":True},
                )

            cur.execute(
                """
                SELECT instance_id,generation,mode,background_epoch
                FROM persona_selection WHERE user_id=%s FOR SHARE
                """,
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if (
                selected is None or selected["instance_id"] != command.instance_id
                or selected["generation"] != command.expected_generation
            ):
                raise StaleSelection("enqueue selection generation invalid")

            # Check mutable input versions only for a NEW admission, after
            # idempotency replay and while holding the selection authority gate.
            if "persona_instance" in command.input_versions:
                cur.execute(
                    "SELECT version FROM persona_instances "
                    "WHERE user_id=%s AND id=%s FOR SHARE",
                    (actor.user_id, command.instance_id),
                )
                current_instance = cur.fetchone()
                if (current_instance is None or current_instance["version"]
                    != command.input_versions["persona_instance"]):
                    raise VersionConflict("enqueue input state version already changed")

            # A historical retry already returned above, even if its deadline
            # has now elapsed. New admissions must still have a future deadline.
            if command.deadline is not None and command.deadline <= datetime.now(timezone.utc):
                raise InvalidOperation("operation deadline has already passed")
            if (not platform and command.origin is not Origin.USER
                and not cross_origin):
                raise PermissionDenied("agent-originated operation requires a platform actor")

            # Bind ephemeral work to one Background epoch from admission.
            # An operation queued in Foreground targets only the next epoch.
            admitted_background_epoch = None
            if command.execution_policy == Policy.BACKGROUND_EPHEMERAL:
                admitted_background_epoch = selected["background_epoch"] + (
                    0 if selected["mode"] == "BACKGROUND" else 1
                )

            for dependency_id in command.dependencies:
                cur.execute(
                    """
                    SELECT 1 FROM operations
                    WHERE operation_id=%s AND user_id=%s AND instance_id=%s
                    """,
                    (dependency_id,actor.user_id,command.instance_id),
                )
                if cur.fetchone() is None:
                    raise PermissionDenied("dependency not owned by this persona instance")
            for key,units in sorted(command.resources.items()):
                cur.execute(
                    """
                    SELECT capacity_units,p0_reserved_units
                    FROM execution_resource_pools WHERE resource_key=%s FOR SHARE
                    """,
                    (key,),
                )
                pool=cur.fetchone()
                if pool is None:
                    raise ResourceUnavailable(f"resource pool not configured: {key}")
                usable=(pool["capacity_units"] if command.priority_class == Priority.P0
                        else pool["capacity_units"]-pool["p0_reserved_units"])
                if units > usable:
                    raise ResourceUnavailable(f"request exceeds available {key} tier capacity")

            cur.execute(
                """
                INSERT INTO operations (
                    operation_id,user_id,instance_id,operation_type,origin,
                    execution_policy,priority_class,eligible_at,deadline,
                    timeout_seconds,max_attempts,selection_generation,
                    background_epoch,effect_class,request_fingerprint,source_id,
                    idempotency_key,input_versions,concurrency_group
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,coalesce(%s,clock_timestamp()),%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s
                )
                RETURNING *
                """,
                (
                    command.operation_id,actor.user_id,command.instance_id,
                    command.operation_type,command.origin.value,
                    command.execution_policy.value,command.priority_class.value,
                    command.eligible_at,command.deadline,command.timeout_seconds,
                    command.max_attempts,command.expected_generation,
                    admitted_background_epoch,command.effect_class.value,
                    fingerprint,actor.source_id,
                    command.idempotency_key,json.dumps(command.input_versions),
                    command.concurrency_group,
                ),
            )
            operation=cur.fetchone()
            for key,units in sorted(command.resources.items()):
                cur.execute(
                    """
                    INSERT INTO operation_resource_requests(operation_id,resource_key,units)
                    VALUES (%s,%s,%s)
                    """,
                    (command.operation_id,key,units),
                )
            for cap in sorted(command.capabilities):
                cur.execute(
                    """
                    INSERT INTO operation_capabilities(operation_id,capability)
                    VALUES (%s,%s)
                    """,
                    (command.operation_id,cap),
                )
            for dependency_id in command.dependencies:
                cur.execute(
                    """
                    INSERT INTO operation_dependencies(operation_id,depends_on_id)
                    VALUES (%s,%s)
                    """,
                    (command.operation_id,dependency_id),
                )
            event_id=event_for(
                cur,operation,"operation.queued.v1",
                {"status":"QUEUED","origin":command.origin.value,
                 "execution_policy":command.execution_policy.value,
                 "background_epoch":admitted_background_epoch},
                source_id=actor.source_id,
                trust_level="TRUSTED_PLATFORM" if platform else "AUTHENTICATED_USER",
            )
            result=EnqueueResult(
                operation_id=command.operation_id,event_id=event_id,
                status=Status.QUEUED,
            )
            cur.execute(
                """
                UPDATE command_receipts
                SET event_id=%s,result_json=%s::jsonb WHERE receipt_id=%s
                """,
                (event_id,result.model_dump_json(),receipt_id),
            )
            return result


def _eligible_for_policy(cursor, operation: dict, selected: dict | None) -> bool:
    if (
        selected is None or selected["instance_id"] != operation["instance_id"]
        or selected["generation"] != operation["selection_generation"]
    ):
        return False
    if operation["execution_policy"] == Policy.FOREGROUND_ASYNC.value:
        return selected["mode"] in ("FOREGROUND","BACKGROUND")
    if selected["mode"] != "BACKGROUND":
        return False
    if operation["execution_policy"] == Policy.BACKGROUND_EPHEMERAL.value:
        return operation["background_epoch"] == selected["background_epoch"]
    if (operation["background_epoch"] is not None
        and operation["background_epoch"] != selected["background_epoch"]):
        return False
    # P2 requires a real epoch-specific Background preparation barrier.
    cursor.execute(
        """
        SELECT 1 FROM background_ready_epochs
        WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
        """,
        (
            operation["user_id"],operation["instance_id"],
            selected["background_epoch"],
        ),
    )
    return cursor.fetchone() is not None


def claim_operation(
    connection: Connection,
    *,
    worker_id: str,
    capabilities: tuple[str,...]=(),
    lease_seconds: int=30,
    scan_limit: int=40,
    operation_types: tuple[str,...] | None=None,
) -> OperationLease | None:
    """Admission under row locks: lifecycle, deps, capabilities and resources."""
    require_idle(connection)
    if not 1 <= len(worker_id) <= 128:
        raise ValueError("worker_id must contain 1–128 characters")
    if not 1 <= lease_seconds <= 3600 or not 1 <= scan_limit <= 200:
        raise ValueError("invalid lease_seconds or scan_limit")
    if operation_types is not None and (
        not operation_types or any(not 1 <= len(t) <= 128 for t in operation_types)
    ):
        raise ValueError("invalid operation_types filter")
    allowed_types=list(operation_types) if operation_types is not None else None

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            # scan_limit is PAGE size, not a global truncation. Older operations
            # blocked on resources or Background mode must never hide later
            # runnable work (including work for other users/worker types).
            # Keyset pagination keeps a stable priority/order cursor and avoids
            # re-reading the same ineligible prefix on every page.
            cursor_key = (None, None, None, None)
            while True:
                cur.execute(
                    """
                    WITH eligible AS (
                        SELECT o.operation_id, o.eligible_at, o.created_at,
                               CASE o.priority_class WHEN 'P0' THEN 0
                                                     WHEN 'P1' THEN 1 ELSE 2
                               END AS priority_rank
                        FROM operations o
                        WHERE o.status IN ('QUEUED','RETRY_WAIT')
                          AND (%s::text[] IS NULL OR o.operation_type=ANY(%s::text[]))
                          AND o.attempt_count < o.max_attempts
                          AND o.eligible_at <= clock_timestamp()
                          AND (o.deadline IS NULL OR o.deadline > clock_timestamp())
                          AND NOT EXISTS (
                              SELECT 1 FROM operation_capabilities required
                              WHERE required.operation_id = o.operation_id
                                AND NOT (required.capability = ANY(%s::text[]))
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM operation_dependencies dep
                              JOIN operations parent
                                  ON parent.operation_id = dep.depends_on_id
                              WHERE dep.operation_id = o.operation_id
                                AND parent.status <> 'COMPLETED'
                          )
                    )
                    SELECT operation_id, priority_rank, eligible_at, created_at
                    FROM eligible
                    WHERE (
                        %s::integer IS NULL OR
                        (priority_rank, eligible_at, created_at, operation_id)
                            > (%s::integer, %s::timestamptz,
                               %s::timestamptz, %s::uuid)
                    )
                    ORDER BY priority_rank, eligible_at, created_at, operation_id
                    LIMIT %s
                    """,
                    (allowed_types,allowed_types,list(capabilities),
                     cursor_key[0], *cursor_key, scan_limit),
                )
                page = cur.fetchall()
                if not page:
                    return None
                for candidate in page:
                    op_id = candidate["operation_id"]
                    cur.execute(
                        """
                        SELECT * FROM operations
                        WHERE operation_id=%s
                          AND (%s::text[] IS NULL OR operation_type=ANY(%s::text[]))
                          AND status IN ('QUEUED','RETRY_WAIT')
                          AND attempt_count < max_attempts
                          AND eligible_at <= clock_timestamp()
                          AND (deadline IS NULL OR deadline > clock_timestamp())
                        FOR UPDATE SKIP LOCKED
                        """,
                        (op_id,allowed_types,allowed_types),
                    )
                    op=cur.fetchone()
                    if op is None:
                        continue
                    cur.execute(
                        "SELECT capability FROM operation_capabilities WHERE operation_id=%s",
                        (op_id,),
                    )
                    if not {r["capability"] for r in cur.fetchall()}.issubset(capabilities):
                        continue
                    cur.execute(
                        """
                        SELECT 1 FROM operation_dependencies dep
                        JOIN operations parent ON parent.operation_id=dep.depends_on_id
                        WHERE dep.operation_id=%s AND parent.status <> 'COMPLETED'
                        LIMIT 1
                        """,
                        (op_id,),
                    )
                    if cur.fetchone() is not None:
                        continue
                    cur.execute(
                        """
                        SELECT instance_id,generation,mode,background_epoch
                        FROM persona_selection WHERE user_id=%s FOR SHARE
                        """,
                        (op["user_id"],),
                    )
                    selected=cur.fetchone()
                    if not _eligible_for_policy(cur,op,selected):
                        continue
                    if "persona_instance" in op["input_versions"]:
                        cur.execute(
                            "SELECT version FROM persona_instances "
                            "WHERE id=%s AND user_id=%s FOR SHARE",
                            (op["instance_id"],op["user_id"]),
                        )
                        state_version=cur.fetchone()
                        if (state_version is None or
                            state_version["version"]!=op["input_versions"]["persona_instance"]):
                            continue
                    # Selection and input-state locks may have waited until
                    # after the admission deadline without revoking authority.
                    if op["deadline"] is not None:
                        cur.execute("SELECT clock_timestamp() AS server_now")
                        if op["deadline"] <= cur.fetchone()["server_now"]:
                            continue
                    cur.execute(
                        """
                        SELECT resource_key,units FROM operation_resource_requests
                        WHERE operation_id=%s ORDER BY resource_key
                        """,
                        (op_id,),
                    )
                    req=cur.fetchall()
                    if not req:
                        raise InvalidOperation("operation missing resource request")
                    pools=[]
                    allowed=True
                    for requested in req:
                        cur.execute(
                            """
                            SELECT resource_key,capacity_units,p0_reserved_units,active_units,
                                   active_non_p0_units
                            FROM execution_resource_pools
                            WHERE resource_key=%s FOR UPDATE SKIP LOCKED
                            """,
                            (requested["resource_key"],),
                        )
                        pool=cur.fetchone()
                        if pool is None:
                            allowed=False
                            break
                        # All priorities share physical capacity. Only P1/P2
                        # usage counts against the non-reserved quota.
                        # P0 activity must not consume that quota merely
                        # because it was claimed before a P1 operation.
                        if pool["active_units"]+requested["units"]>pool["capacity_units"]:
                            allowed=False
                            break
                        if (
                            op["priority_class"]!="P0"
                            and pool["active_non_p0_units"]+requested["units"]
                                >pool["capacity_units"]-pool["p0_reserved_units"]
                        ):
                            allowed=False
                            break
                        pools.append((pool,requested["units"]))
                    if not allowed:
                        continue
                    # Admission writes must be reversible when the final SQL
                    # deadline guard loses a race after pool/group acquisition.
                    cur.execute("SAVEPOINT claim_candidate")
                    if op["concurrency_group"] is not None:
                        cur.execute(
                            """
                            INSERT INTO operation_concurrency_locks(
                                user_id,instance_id,group_key,operation_id,claim_generation
                            ) VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT DO NOTHING RETURNING operation_id
                            """,
                            (
                                op["user_id"],op["instance_id"],op["concurrency_group"],
                                op_id,op["claim_generation"]+1,
                            ),
                        )
                        if cur.fetchone() is None:
                            cur.execute("ROLLBACK TO SAVEPOINT claim_candidate")
                            cur.execute("RELEASE SAVEPOINT claim_candidate")
                            continue
                    for pool,units in pools:
                        cur.execute(
                            """
                            UPDATE execution_resource_pools
                            SET active_units=active_units+%s,
                                active_non_p0_units=active_non_p0_units+%s
                            WHERE resource_key=%s
                            """,
                            (units, units if op["priority_class"]!="P0" else 0,
                             pool["resource_key"]),
                        )
                        cur.execute(
                            """
                            INSERT INTO operation_resource_reservations(
                                operation_id,resource_key,claim_generation,units
                            ) VALUES (%s,%s,%s,%s)
                            """,
                            (op_id,pool["resource_key"],op["claim_generation"]+1,units),
                        )
                    cur.execute(
                        """
                        UPDATE operations SET
                            status='RUNNING',worker_id=%s,
                            claim_generation=claim_generation+1,
                            attempt_count=attempt_count+1,
                            lease_until=LEAST(
                                clock_timestamp()+(%s*interval '1 second'),
                                clock_timestamp()+(timeout_seconds*interval '1 second'),
                                COALESCE(deadline,'infinity'::timestamptz)
                            ),
                            claimed_at=clock_timestamp(),
                            background_epoch=%s,
                            version=version+1,updated_at=clock_timestamp()
                        WHERE operation_id=%s
                          AND status IN ('QUEUED','RETRY_WAIT')
                          AND attempt_count < max_attempts
                          AND eligible_at <= clock_timestamp()
                          AND (deadline IS NULL OR deadline > clock_timestamp())
                        RETURNING *
                        """,
                        (
                            worker_id,lease_seconds,
                            None if op["execution_policy"]==Policy.FOREGROUND_ASYNC.value
                            else selected["background_epoch"],
                            op_id,
                        ),
                    )
                    running=cur.fetchone()
                    if running is None:
                        # Revert the claim's group and resource writes.
                        # Without this savepoint rollback, an expired claim
                        # would strand capacity despite never becoming RUNNING.
                        cur.execute("ROLLBACK TO SAVEPOINT claim_candidate")
                        cur.execute("RELEASE SAVEPOINT claim_candidate")
                        continue
                    cur.execute("RELEASE SAVEPOINT claim_candidate")
                    event_for(cur,running,"operation.claimed.v1",{
                        "worker_id":worker_id,
                        "claim_generation":running["claim_generation"],
                        "attempt_count":running["attempt_count"],
                    })
                    return OperationLease(
                        operation_id=op_id,user_id=running["user_id"],
                        instance_id=running["instance_id"],worker_id=worker_id,
                        claim_generation=running["claim_generation"],
                        selection_generation=running["selection_generation"],
                        background_epoch=running["background_epoch"],
                        attempt_count=running["attempt_count"],
                        lease_until=running["lease_until"],
                    )
                last = page[-1]
                cursor_key = (
                    last["priority_rank"], last["eligible_at"],
                    last["created_at"], last["operation_id"],
                )

