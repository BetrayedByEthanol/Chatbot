"""P0-04 typed operation contracts and shared PostgreSQL commit primitives."""

import json
from datetime import datetime, timedelta
from enum import Enum
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg import Connection
from psycopg.pq import TransactionStatus

from .tool_contracts import PROCEDURE_AGENT_OPERATION, SLOW_TOOL, RESOURCE_KEY


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Policy(str, Enum):
    FOREGROUND_ASYNC = "FOREGROUND_ASYNC"
    BACKGROUND_EPHEMERAL = "BACKGROUND_EPHEMERAL"
    BACKGROUND_RESUMABLE = "BACKGROUND_RESUMABLE"


class Origin(str, Enum):
    USER = "USER"
    COGNITIVE = "COGNITIVE"
    GOAL = "GOAL"


class EffectClass(str, Enum):
    NONE = "NONE"
    READ_ONLY = "READ_ONLY"
    EXTERNAL = "EXTERNAL"


class Status(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUSPEND_REQUESTED = "SUSPEND_REQUESTED"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class EnqueueOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    operation_type: str = Field(min_length=1, max_length=128)
    origin: Origin = Origin.USER
    execution_policy: Policy = Policy.FOREGROUND_ASYNC
    priority_class: Priority = Priority.P1
    effect_class: EffectClass = EffectClass.NONE
    resources: dict[str, int] = Field(min_length=1)
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[UUID, ...] = ()
    concurrency_group: str | None = Field(default=None, min_length=1, max_length=128)
    eligible_at: datetime | None = None
    deadline: datetime | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=30)
    input_versions: dict[str, int] = Field(default_factory=dict)
    task_id: UUID | None = None  # Task gate is not implemented yet.

    @model_validator(mode="after")
    def validate_operation(self):
        agent_step=self.operation_type==PROCEDURE_AGENT_OPERATION
        if self.task_id is not None and not agent_step:
            raise ValueError("task-owned operations require the registered procedure gate")
        if agent_step:
            # This is NOT a generic executable task gateway: only the exact
            # already-registered, read-only tool may be admitted through a
            # durable user-approved ProcedureRun/Task/Step authorization.
            if (
                self.task_id is None
                or self.origin is not Origin.USER
                or self.execution_policy is not Policy.FOREGROUND_ASYNC
                or self.priority_class is not Priority.P1
                or self.effect_class is not EffectClass.READ_ONLY
                or self.capabilities!=(SLOW_TOOL,)
                or self.resources!={RESOURCE_KEY:1}
                or self.dependencies or self.eligible_at is not None
                or self.deadline is not None
                or self.timeout_seconds!=300 or self.max_attempts!=3
                or self.concurrency_group!=f"procedure.agent:{self.operation_id}"
                or self.idempotency_key!=f"procedure-agent:{self.operation_id}"
                or set(self.input_versions)
                    !={"task","procedure_run","procedure_step"}
                or any(v<1 for v in self.input_versions.values())
            ):
                raise ValueError("procedure AGENT operation does not match the restricted tool gate")
        if self.execution_policy == Policy.BACKGROUND_EPHEMERAL and self.priority_class != Priority.P1:
            raise ValueError("Background ephemeral operation must use P1")
        if self.execution_policy == Policy.BACKGROUND_RESUMABLE and self.priority_class != Priority.P2:
            raise ValueError("Background resumable operation must use P2")
        if self.priority_class == Priority.P2 and self.execution_policy != Policy.BACKGROUND_RESUMABLE:
            raise ValueError("P2 requires Background resumable policy")
        if self.execution_policy == Policy.FOREGROUND_ASYNC and self.priority_class == Priority.P2:
            raise ValueError("Foreground work cannot be P2")
        if self.effect_class == EffectClass.EXTERNAL and self.origin != Origin.USER:
            raise ValueError("external effects require explicit user-origin admission")
        if len(set(self.dependencies)) != len(self.dependencies) or self.operation_id in self.dependencies:
            raise ValueError("dependency list contains duplicate or self-reference")
        if any(not (1 <= len(name) <= 128) or units <= 0
               for name, units in self.resources.items()):
            raise ValueError("invalid resource key or units")
        if any(not (1 <= len(c) <= 128) for c in self.capabilities):
            raise ValueError("capability names must be 1..128 characters")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("duplicate capabilities")
        if any(v < 0 for v in self.input_versions.values()):
            raise ValueError("negative dependency version")
        permitted_versions=(
            {"task","procedure_run","procedure_step"}
            if agent_step else {"persona_instance"}
        )
        if set(self.input_versions) - permitted_versions:
            raise ValueError("unknown input version owner")
        for dt in (self.eligible_at, self.deadline):
            if dt is not None and (dt.tzinfo is None or dt.utcoffset() is None):
                raise ValueError("timestamps must be timezone-aware")
        # Deadline validity relative to the current clock is checked only
        # after scoped idempotency lookup, so historical retries still replay.
        return self


class EnqueueResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_id: UUID
    event_id: UUID
    status: Status
    replayed: bool = False


class OperationLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_id: UUID
    user_id: UUID
    instance_id: UUID
    worker_id: str
    claim_generation: int
    selection_generation: int
    background_epoch: int | None
    attempt_count: int
    lease_until: datetime


class InvalidOperation(Exception):
    pass


class StaleOperation(Exception):
    pass


class ResourceUnavailable(Exception):
    pass


def require_idle(connection: Connection) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("operation commands require an idle autocommit connection")


def event_for(cursor, row: dict, event_type: str, detail: dict | None = None, *, source_id: str = 'operation.scheduler', trust_level: str = 'TRUSTED_PLATFORM') -> UUID:
    """Append operation domain event + matching outbox inside caller's transaction."""
    operation_id = row["operation_id"]
    instance_id = row["instance_id"]
    user_id = row["user_id"]
    stream_id = uuid5(NAMESPACE_URL, f"persona-agent/operation/{operation_id}")
    cursor.execute(
        """
        INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
        VALUES (%s,%s,%s,%s) ON CONFLICT (stream_id) DO NOTHING
        """,
        (stream_id, user_id, instance_id, f"operation:{operation_id}"),
    )
    cursor.execute(
        """
        UPDATE event_streams SET last_sequence=last_sequence+1
        WHERE stream_id=%s AND user_id=%s AND instance_id=%s
        RETURNING last_sequence
        """,
        (stream_id, user_id, instance_id),
    )
    seq = cursor.fetchone()
    if seq is None:
        raise InvalidOperation("event stream scope mismatch")
    event_id = uuid4()
    cursor.execute(
        """
        INSERT INTO events (
            event_id,event_type,schema_version,user_id,instance_id,
            stream_id,stream_sequence,source_id,trust_level,payload
        ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (
            event_id,event_type,user_id,instance_id,stream_id,
            seq["last_sequence"],source_id,trust_level,
            json.dumps({"operation_id":str(operation_id),**(detail or {})}),
        ),
    )
    cursor.execute(
        "INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
        (uuid4(),event_id),
    )
    return event_id


def lock_operation(cursor, operation_id: UUID) -> dict:
    cursor.execute(
        "SELECT * FROM operations WHERE operation_id=%s FOR UPDATE",
        (operation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise InvalidOperation("operation not found")
    return row


def require_claim(cursor, lease: OperationLease, *, statuses=("RUNNING",)) -> dict:
    """Check full fencing token and current PostgreSQL time under operation lock."""
    row = lock_operation(cursor, lease.operation_id)
    cursor.execute("SELECT clock_timestamp() AS server_now")
    now = cursor.fetchone()["server_now"]
    if (
        row["status"] not in statuses or row["worker_id"] != lease.worker_id
        or row["claim_generation"] != lease.claim_generation
        or row["selection_generation"] != lease.selection_generation
        or row["user_id"] != lease.user_id or row["instance_id"] != lease.instance_id
        or row["background_epoch"] != lease.background_epoch
        or row["lease_until"] is None or row["lease_until"] <= now
        or (row["deadline"] is not None and row["deadline"] <= now)
        or (
            row["claimed_at"] is not None
            and row["claimed_at"] + timedelta(
                seconds=row["timeout_seconds"]
            ) <= now
        )
    ):
        raise StaleOperation("attempt is no longer valid")
    return row


def require_current_authority(cursor, row: dict) -> None:
    """Same row lock as persona selection/switch; reject obsolete generations."""
    cursor.execute(
        """
        SELECT instance_id,generation,mode,background_epoch
        FROM persona_selection WHERE user_id=%s FOR SHARE
        """,
        (row["user_id"],),
    )
    state = cursor.fetchone()
    if (
        state is None or state["instance_id"] != row["instance_id"]
        or state["generation"] != row["selection_generation"]
    ):
        raise StaleOperation("persona execution generation revoked")
    if row["execution_policy"] != Policy.FOREGROUND_ASYNC.value and (
        state["mode"] != "BACKGROUND"
        or state["background_epoch"] != row["background_epoch"]
    ):
        raise StaleOperation("background epoch or mode invalidated")
    if "persona_instance" in row["input_versions"]:
        cursor.execute(
            """
            SELECT version FROM persona_instances
            WHERE user_id=%s AND id=%s FOR SHARE
            """,
            (row["user_id"],row["instance_id"]),
        )
        version=cursor.fetchone()
        if version is None or version["version"] != row["input_versions"]["persona_instance"]:
            raise StaleOperation("operation input state version changed")


def release_resources(cursor, row: dict) -> None:
    """Only callers holding the operation row lock may release its resources."""
    cursor.execute(
        """
        DELETE FROM operation_resource_reservations
        WHERE operation_id=%s RETURNING resource_key,units
        """,
        (row["operation_id"],),
    )
    units = sorted(cursor.fetchall(), key=lambda x:x["resource_key"])
    for reservation in units:
        cursor.execute(
            """
            UPDATE execution_resource_pools
            SET active_units=active_units-%s,
                active_non_p0_units=active_non_p0_units-%s
            WHERE resource_key=%s
              AND active_units >= %s
              AND active_non_p0_units >= %s
            RETURNING resource_key
            """,
            (
                reservation["units"],
                reservation["units"] if row["priority_class"]!="P0" else 0,
                reservation["resource_key"],reservation["units"],
                reservation["units"] if row["priority_class"]!="P0" else 0,
            ),
        )
        if cursor.fetchone() is None:
            raise InvalidOperation("resource reservation accounting mismatch")
    cursor.execute(
        "DELETE FROM operation_concurrency_locks WHERE operation_id=%s",
        (row["operation_id"],),
    )
