"""P4-04 authorized Background selection (not an independent scheduler).

Only an authenticated, separately approved, single-epoch Procedure stage can
be dispatched. The P0 operation scheduler owns admission, claim, capacity,
lease and execution. This component scans READY epochs and picks at most one
already-authorized AGENT step per tick, without model-generated permissions.
"""
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .lifecycle import TrustedPlatformActor
from .operation_contracts import (
    EffectClass, EnqueueOperation, Origin, Policy, Priority,
    ResourceUnavailable, require_idle,
)
from .operation_scheduler import enqueue_operation
from .runtime import PermissionDenied, StaleSelection, VersionConflict
from .tool_contracts import PROCEDURE_AGENT_OPERATION, RESOURCE_KEY, SLOW_TOOL

_SOURCE="background.autonomous.dispatch"


def dispatch_one_background_step(connection: Connection,
                                 actor: TrustedPlatformActor,
                                 instance_id: UUID,
                                 background_epoch: int):
    """Select one approved eligible stage; P0 rechecks all authority to enqueue.

    Empty result is normal: no user-approved step is awaiting dispatch.
    When a source version or lifecycle changes between selection and P0
    enqueue, P0 rejects the stale operation. Nothing is silently reapproved.
    """
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor or actor.source_id!=_SOURCE:
        raise PermissionDenied("only the autonomous Background producer can dispatch")
    if background_epoch<1:
        raise ValueError("invalid Background epoch")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT instance_id,generation,mode,background_epoch
                   FROM persona_selection WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if (selected is None or selected["instance_id"]!=instance_id
                or selected["mode"]!="BACKGROUND"
                or selected["background_epoch"]!=background_epoch):
                raise StaleSelection("Background epoch no longer selected")
            cur.execute(
                """SELECT e.autonomous_cursor_position
                   FROM background_preparation_epochs e
                   JOIN background_alignment_certificates cert
                     ON cert.user_id=e.user_id AND cert.instance_id=e.instance_id
                    AND cert.background_epoch=e.background_epoch
                   JOIN background_ready_epochs ready
                     ON ready.user_id=e.user_id AND ready.instance_id=e.instance_id
                    AND ready.background_epoch=e.background_epoch
                   WHERE e.user_id=%s AND e.instance_id=%s
                     AND e.background_epoch=%s
                     AND e.selection_generation=%s
                     AND e.status='PENDING' AND e.preparation_stage='READY'
                   FOR UPDATE OF e""",
                (actor.user_id,instance_id,background_epoch,selected["generation"]),
            )
            epoch_state=cur.fetchone()
            if epoch_state is None:
                return None
            # Persist an updated polling timestamp even with no runnable
            # grants or available P2 slots; a fixed READY prefix must never
            # starve other personas.
            cur.execute(
                """UPDATE background_preparation_epochs
                   SET autonomous_polled_at=clock_timestamp()
                   WHERE user_id=%s AND instance_id=%s AND background_epoch=%s""",
                (actor.user_id,instance_id,background_epoch),
            )
            # At most two active P2 operations per persona/epoch; P0 checks
            # this budget AGAIN transactionally when enqueuing.
            cur.execute(
                """SELECT count(*) AS n FROM operations
                   WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                     AND priority_class='P2'
                     AND status IN ('QUEUED','RETRY_WAIT','RUNNING',
                                    'CANCEL_REQUESTED','SUSPEND_REQUESTED')""",
                (actor.user_id,instance_id,background_epoch),
            )
            if cur.fetchone()["n"]>=2:
                return None
            cur.execute(
                """SELECT stage.operation_id,stage.task_id,stage.run_version,
                          stage.step_version,stage.task_version,
                          checked.priority_rank
                   FROM procedure_agent_operation_authorizations stage
                   JOIN background_alignment_task_checks checked
                     ON checked.user_id=stage.user_id
                    AND checked.instance_id=stage.instance_id
                    AND checked.background_epoch=stage.approved_background_epoch
                    AND checked.task_id=stage.task_id
                   JOIN goal_tasks task ON task.task_id=stage.task_id
                     AND task.user_id=stage.user_id
                     AND task.instance_id=stage.instance_id
                   JOIN procedure_runs run ON run.run_id=stage.run_id
                     AND run.user_id=stage.user_id AND run.instance_id=stage.instance_id
                   JOIN procedure_run_steps step ON step.run_id=stage.run_id
                     AND step.step_id=stage.step_id
                     AND step.user_id=stage.user_id AND step.instance_id=stage.instance_id
                   LEFT JOIN operations existing
                     ON existing.operation_id=stage.operation_id
                   WHERE stage.user_id=%s AND stage.instance_id=%s
                     AND stage.selection_generation=%s
                     AND stage.approved_background_epoch=%s
                     AND stage.state='AUTHORIZED'
                     AND checked.decision='ELIGIBLE'
                     AND checked.task_version=stage.task_version
                     AND task.status='PLANNED' AND NOT task.suspended
                     AND checked.task_version=task.version
                     AND (task.deadline IS NULL OR task.deadline>clock_timestamp())
                     AND run.status='ACTIVE' AND run.version=stage.run_version
                     AND step.executor='AGENT' AND step.capability=%s
                     AND step.version=stage.step_version
                     AND step.status IN ('NOT_STARTED','FAILED','BLOCKED')
                     AND existing.operation_id IS NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM operations running
                       WHERE running.user_id=stage.user_id
                         AND running.instance_id=stage.instance_id
                         AND running.task_id=stage.task_id
                         AND running.status IN ('QUEUED','RUNNING','RETRY_WAIT',
                                                'CANCEL_REQUESTED','SUSPEND_REQUESTED')
                     )
                   ORDER BY checked.priority_rank,
                            (SELECT count(*) FROM goal_tasks preferred
                             WHERE preferred.user_id=task.user_id
                               AND preferred.instance_id=task.instance_id
                               AND preferred.preferred_over_task_id=task.task_id
                               AND preferred.status='PLANNED'
                               AND NOT preferred.suspended),
                            task.deadline NULLS LAST,stage.task_id,stage.operation_id
                   LIMIT 1 OFFSET %s""",
                (actor.user_id,instance_id,selected["generation"],
                 background_epoch,SLOW_TOOL,epoch_state["autonomous_cursor_position"]),
            )
            stage=cur.fetchone()
            cur.execute(
                """UPDATE background_preparation_epochs
                   SET autonomous_cursor_position=%s
                   WHERE user_id=%s AND instance_id=%s AND background_epoch=%s""",
                (epoch_state["autonomous_cursor_position"]+1 if stage else 0,
                 actor.user_id,instance_id,background_epoch),
            )
    if stage is None:
        return None
    command=EnqueueOperation(
        operation_id=stage["operation_id"],instance_id=instance_id,
        expected_generation=selected["generation"],
        idempotency_key=f"procedure-agent:{stage['operation_id']}",
        operation_type=PROCEDURE_AGENT_OPERATION,origin=Origin.GOAL,
        execution_policy=Policy.BACKGROUND_RESUMABLE,
        priority_class=Priority.P2,effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},capabilities=(SLOW_TOOL,),
        concurrency_group=f"procedure.agent:{stage['operation_id']}",
        input_versions={
            "task":stage["task_version"],
            "procedure_run":stage["run_version"],
            "procedure_step":stage["step_version"],
        },
        task_id=stage["task_id"],
    )
    return enqueue_operation(connection,actor,command)


def run_autonomous_scan(connection: Connection, *, limit: int=32) -> tuple:
    """One bounded, fair producer pass; P0 still performs every claim."""
    require_idle(connection)
    if not 1<=limit<=128:
        raise ValueError("invalid autonomous scan limit")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT s.user_id,s.instance_id,s.background_epoch
               FROM persona_selection s
               JOIN background_preparation_epochs e ON e.user_id=s.user_id
                 AND e.instance_id=s.instance_id AND e.background_epoch=s.background_epoch
                 AND e.selection_generation=s.generation
               JOIN background_ready_epochs ready ON ready.user_id=e.user_id
                 AND ready.instance_id=e.instance_id AND ready.background_epoch=e.background_epoch
               WHERE s.mode='BACKGROUND' AND e.status='PENDING'
                 AND e.preparation_stage='READY'
               ORDER BY e.autonomous_polled_at NULLS FIRST,
                        ready.ready_at,s.user_id
               LIMIT %s""",(limit,),
        )
        ready=tuple(cur.fetchall())
    results=[]
    for epoch in ready:
        try:
            result=dispatch_one_background_step(
                connection,TrustedPlatformActor(
                    user_id=epoch["user_id"],source_id=_SOURCE),
                epoch["instance_id"],epoch["background_epoch"],
            )
        except (StaleSelection,PermissionDenied,VersionConflict,ResourceUnavailable):
            # The source is still persisted; operator/owner can resolve a
            # changed approval, or next polling tick can retry resource loss.
            continue
        if result is not None:
            results.append(result)
    return tuple(results)
