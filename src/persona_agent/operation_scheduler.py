"""P0-04 operation admission: scoped enqueue, capability/dependency checks, fenced claim."""

import json
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .operation_contracts import (
    EffectClass, EnqueueOperation, EnqueueResult, InvalidOperation,
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
            RETURNING resource_key
            """,
            (resource_key,capacity_units,p0_reserved_units),
        )
        if cursor.fetchone() is None:
            raise ResourceUnavailable("cannot shrink pool below active reservations")


def enqueue_operation(
    connection: Connection,
    actor: TrustedActor,
    command: EnqueueOperation,
) -> EnqueueResult:
    """Idempotent admission to durable queue; actual worker eligibility is rechecked."""
    require_idle(connection)
    if type(actor) is not TrustedActor:
        raise PermissionDenied("enqueue requires authenticated primary user")
    fingerprint = _fingerprint(command)
    receipt_id=uuid4()

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT version FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            instance=cur.fetchone()
            if instance is None:
                raise PermissionDenied("operation instance outside authenticated scope")
            if ("persona_instance" in command.input_versions
                and instance["version"] != command.input_versions["persona_instance"]):
                raise VersionConflict("enqueue input state version already changed")
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
                SELECT instance_id,generation,mode
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

            if command.priority_class == Priority.P2 and command.origin.value != "USER":
                # Autonomous goal planning/permission must be implemented before
                # agent-originated work can be authorized.
                raise PermissionDenied("agent-originated autonomy is not authorized in P0-04")

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
                    effect_class,request_fingerprint,source_id,idempotency_key,
                    input_versions,concurrency_group
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,coalesce(%s,clock_timestamp()),%s,
                    %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s
                )
                RETURNING *
                """,
                (
                    command.operation_id,actor.user_id,command.instance_id,
                    command.operation_type,command.origin.value,
                    command.execution_policy.value,command.priority_class.value,
                    command.eligible_at,command.deadline,command.timeout_seconds,
                    command.max_attempts,command.expected_generation,
                    command.effect_class.value,fingerprint,actor.source_id,
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
                 "execution_policy":command.execution_policy.value},
                source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
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
    if (operation["background_epoch"] is not None
        and operation["background_epoch"] != selected["background_epoch"]):
        return False
    if operation["execution_policy"] == Policy.BACKGROUND_EPHEMERAL.value:
        return True
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
) -> OperationLease | None:
    """Admission under row locks: lifecycle, deps, capabilities and resources."""
    require_idle(connection)
    if not 1 <= len(worker_id) <= 128:
        raise ValueError("worker_id must contain 1–128 characters")
    if not 1 <= lease_seconds <= 3600 or not 1 <= scan_limit <= 200:
        raise ValueError("invalid lease_seconds or scan_limit")

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT operation_id FROM operations
                WHERE status IN ('QUEUED','RETRY_WAIT')
                  AND eligible_at <= clock_timestamp()
                  AND (deadline IS NULL OR deadline > clock_timestamp())
                ORDER BY CASE priority_class WHEN 'P0' THEN 0
                                             WHEN 'P1' THEN 1 ELSE 2 END,
                         eligible_at,created_at,operation_id
                LIMIT %s
                """,
                (scan_limit,),
            )
            candidates=[r["operation_id"] for r in cur.fetchall()]
            for op_id in candidates:
                cur.execute(
                    """
                    SELECT * FROM operations
                    WHERE operation_id=%s
                      AND status IN ('QUEUED','RETRY_WAIT')
                      AND eligible_at <= clock_timestamp()
                      AND (deadline IS NULL OR deadline > clock_timestamp())
                    FOR UPDATE SKIP LOCKED
                    """,
                    (op_id,),
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
                        SELECT resource_key,capacity_units,p0_reserved_units,active_units
                        FROM execution_resource_pools
                        WHERE resource_key=%s FOR UPDATE SKIP LOCKED
                        """,
                        (requested["resource_key"],),
                    )
                    pool=cur.fetchone()
                    if pool is None:
                        allowed=False
                        break
                    limit=(pool["capacity_units"] if op["priority_class"]=="P0"
                           else pool["capacity_units"]-pool["p0_reserved_units"])
                    if pool["active_units"]+requested["units"]>limit:
                        allowed=False
                        break
                    pools.append((pool,requested["units"]))
                if not allowed:
                    continue
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
                        continue
                for pool,units in pools:
                    cur.execute(
                        """
                        UPDATE execution_resource_pools
                        SET active_units=active_units+%s
                        WHERE resource_key=%s
                        """,
                        (units,pool["resource_key"]),
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
                            clock_timestamp()+(timeout_seconds*interval '1 second')
                        ),
                        claimed_at=clock_timestamp(),
                        background_epoch=%s,
                        version=version+1,updated_at=clock_timestamp()
                    WHERE operation_id=%s RETURNING *
                    """,
                    (
                        worker_id,lease_seconds,
                        None if op["execution_policy"]==Policy.FOREGROUND_ASYNC.value
                        else selected["background_epoch"],
                        op_id,
                    ),
                )
                running=cur.fetchone()
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
            return None
