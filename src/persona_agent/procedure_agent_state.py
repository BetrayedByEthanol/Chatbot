"""P3-04: approved AGENT steps through the existing P0 operation scheduler.

Scope: registered, deterministic READ_ONLY text.statistics.v1 only.
No generic tool runner, self-granted capabilities, external effects, or Task
completion. Persist approval BEFORE enqueue; P0 rechecks authority on enqueue,
claim and completion; reconciliation fences accepted step evidence.
"""
import json
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .operation_contracts import (
    EffectClass, EnqueueOperation, Origin, Policy, Priority,
    require_claim, require_current_authority, require_idle, StaleOperation,
)
from .operation_scheduler import claim_operation, enqueue_operation
from .operation_control import complete_operation, fail_operation
from .procedure_agent_contracts import (
    AgentStepAuthorizationResult, AgentStepReconcileResult,
    AuthorizeAgentStepCommand, AuthorizeBackgroundAgentStepCommand,
)
from .procedure_agent_gate import require_procedure_agent_gate
from .procedure_run_state import _run, _step
from .procedure_state import _command, _publish
from .runtime import PermissionDenied, StaleSelection, VersionConflict
from .task_state import _graph_read_lock
from .tool_contracts import (
    PROCEDURE_AGENT_OPERATION, RESOURCE_KEY, SLOW_TOOL,
)
from .tool_gateway import analyze_text


def _result_ref(operation_id: UUID, claim_generation: int) -> str:
    return f"procedure-result:{operation_id}:{claim_generation}"


def authorize_agent_step(connection: Connection, actor: TrustedActor,
                         command: AuthorizeAgentStepCommand
                         ) -> AgentStepAuthorizationResult:
    """Create exact, approved stage; do not enqueue until separately committed."""
    background=type(command) is AuthorizeBackgroundAgentStepCommand
    if type(command) not in (AuthorizeAgentStepCommand,AuthorizeBackgroundAgentStepCommand):
        raise PermissionDenied("unsupported step authorization command")
    def apply(cur,receipt):
        if background:
            cur.execute(
                """SELECT background_epoch,mode,generation FROM persona_selection
                   WHERE user_id=%s AND instance_id=%s FOR SHARE""",
                (actor.user_id,command.instance_id),
            )
            selected=cur.fetchone()
            if (selected is None or selected["mode"]!="FOREGROUND"
                or selected["generation"]!=command.expected_generation
                or selected["background_epoch"]+1!=command.expected_background_epoch):
                raise StaleSelection("Background permission is only valid for the next epoch")
        # All admission paths acquire stage -> graph -> run/step. In
        # particular, do not lock run and then wait on a staging row:
        # P0 enqueue/claim already locks the stage before the run.
        cur.execute(
            """SELECT operation_id,approved_background_epoch
               FROM procedure_agent_operation_authorizations
               WHERE run_id=%s AND step_id=%s AND state='AUTHORIZED'
               FOR UPDATE""",
            (command.run_id,command.step_id),
        )
        pending=cur.fetchone()
        _graph_read_lock(cur,actor,command.instance_id)
        run=_run(cur,actor,command.instance_id,command.run_id)
        if run["status"]!="ACTIVE" or run["version"]!=command.expected_run_version:
            raise VersionConflict("run not active or expected revision stale")
        if run["task_id"] is None:
            raise PermissionDenied("AGENT step requires a task-bound procedure run")
        step=_step(cur,command.run_id,command.step_id)
        if (step["version"]!=command.expected_step_version
            or step["executor"]!="AGENT" or step["capability"]!=SLOW_TOOL):
            raise PermissionDenied("procedure step lacks the registered read-only capability")
        if pending is not None:
            cur.execute(
                "SELECT status FROM operations WHERE operation_id=%s",
                (pending["operation_id"],),
            )
            old_operation=cur.fetchone()
            if old_operation is not None and not (
                background
                and pending["approved_background_epoch"] is not None
                and old_operation["status"]=="BLOCKED"
            ):
                raise VersionConflict(
                    "step has an admitted operation; cancel/reconcile it before replacing"
                )
            # A P0-blocked, never-executed Background operation can be
            # replaced only through NEW authenticated user source/approval.
            # Completed results must be reconciled instead of overwritten.
            # Stage-before-enqueue may crash or encounter a newly revoked
            # Task/Run authorization. A NEW separately approved user command
            # may supersede its orphaned, nonexecutable stage atomically.
            superseded=_publish(
                cur,actor,command.instance_id,"procedure.agent.stage_superseded.v1",{
                    "prior_operation_id":str(pending["operation_id"]),
                    "new_operation_id":str(command.operation_id),
                    "run_id":str(command.run_id),"step_id":command.step_id,
                },receipt,
            )
            cur.execute(
                """UPDATE procedure_agent_operation_authorizations
                   SET state='INVALIDATED',result_event_id=%s
                   WHERE operation_id=%s""",
                (superseded,pending["operation_id"]),
            )
        cur.execute(
            "SELECT 1 FROM procedure_agent_operation_authorizations WHERE operation_id=%s",
            (command.operation_id,),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("operation identifier already allocated")
        event=_publish(cur,actor,command.instance_id,
                       ("procedure.agent.background_authorized.v1" if background
                        else "procedure.agent.authorized.v1"),{
            "operation_id":str(command.operation_id),
            "run_id":str(command.run_id),
            "step_id":command.step_id,
            "task_id":str(run["task_id"]),
            "run_version":command.expected_run_version,
            "step_version":command.expected_step_version,
            "task_version":command.expected_task_version,
            "capability":SLOW_TOOL,
            "effect_class":"READ_ONLY",
            "expected_background_epoch":(command.expected_background_epoch
                                          if background else None),
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO procedure_agent_operation_authorizations(
                 operation_id,user_id,instance_id,run_id,step_id,task_id,
                 run_version,step_version,task_version,selection_generation,
                 capability,max_words,source_conversation_id,source_turn_id,
                 source_quote,approval_turn_id,authorization_event_id,
                 approved_background_epoch
               ) VALUES(
                 %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
               )""",
            (command.operation_id,actor.user_id,command.instance_id,
             command.run_id,command.step_id,run["task_id"],
             command.expected_run_version,command.expected_step_version,
             command.expected_task_version,command.expected_generation,
             SLOW_TOOL,command.max_words,command.conversation_id,
             command.evidence_turn_id,command.evidence_quote,
             command.approval_turn_id,event,
             command.expected_background_epoch if background else None),
        )
        # Use the identical P0 fence before accepting this stage. A failed
        # gate rolls back its approval journal, receipt and staging row.
        require_procedure_agent_gate(cur,{
            "operation_id":command.operation_id,
            "user_id":actor.user_id,
            "instance_id":command.instance_id,
            "task_id":run["task_id"],
            "operation_type":PROCEDURE_AGENT_OPERATION,
            "selection_generation":command.expected_generation,
            "input_versions":{
                "task":command.expected_task_version,
                "procedure_run":command.expected_run_version,
                "procedure_step":command.expected_step_version,
            },
        },stage_only=True)
        return AgentStepAuthorizationResult(
            operation_id=command.operation_id,run_id=command.run_id,
            task_id=run["task_id"],event_id=event,
        )
    return _command(
        connection,actor,command,
        ("procedure.agent.authorize_background.v1" if background
         else "procedure.agent.authorize.v1"),apply,
        result_type=AgentStepAuthorizationResult,
    )


def authorize_background_agent_step(connection: Connection, actor: TrustedActor,
                                    command: AuthorizeBackgroundAgentStepCommand
                                    ) -> AgentStepAuthorizationResult:
    """Authenticated P4-04 one-epoch permission, NEVER immediate dispatch."""
    if type(command) is not AuthorizeBackgroundAgentStepCommand:
        raise PermissionDenied("explicit Background authorization required")
    return authorize_agent_step(connection,actor,command)


def schedule_agent_step(connection: Connection, actor: TrustedActor,
                        command: AuthorizeAgentStepCommand):
    """Crash-recoverable stage-before-enqueue, using P0's ordinary receipt."""
    authorized=authorize_agent_step(connection,actor,command)
    operation=EnqueueOperation(
        operation_id=authorized.operation_id,
        instance_id=command.instance_id,
        expected_generation=command.expected_generation,
        idempotency_key=f"procedure-agent:{authorized.operation_id}",
        operation_type=PROCEDURE_AGENT_OPERATION,
        origin=Origin.USER,
        execution_policy=Policy.FOREGROUND_ASYNC,
        priority_class=Priority.P1,
        effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},
        capabilities=(SLOW_TOOL,),
        concurrency_group=f"procedure.agent:{authorized.operation_id}",
        input_versions={
            "task":command.expected_task_version,
            "procedure_run":command.expected_run_version,
            "procedure_step":command.expected_step_version,
        },
        task_id=authorized.task_id,
    )
    return enqueue_operation(connection,actor,operation)


def _read_authorized_execution(cur, lease):
    row=require_claim(cur,lease)
    require_current_authority(cur,row)
    stage=require_procedure_agent_gate(cur,row)
    cur.execute(
        """SELECT instruction FROM procedure_run_steps
           WHERE run_id=%s AND step_id=%s""",
        (stage["run_id"],stage["step_id"]),
    )
    item=cur.fetchone()
    if item is None:
        raise VersionConflict("AGENT step input vanished")
    # Selection, goal, graph and step locks may have blocked past either
    # the P0 lease or the Task deadline. Recheck BOTH against PostgreSQL's
    # live clock after those waits, immediately before dispatch admission.
    # operations.deadline is intentionally NULL for this registered tool.
    require_claim(cur,lease)
    cur.execute(
        """SELECT (deadline IS NULL OR deadline>clock_timestamp()) AS eligible
           FROM goal_tasks
           WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
        (stage["task_id"],row["user_id"],row["instance_id"]),
    )
    deadline=cur.fetchone()
    if deadline is None or not deadline["eligible"]:
        raise VersionConflict("task deadline expired before AGENT dispatch")
    # No LLM/tool runs while these locks are held.
    return item["instruction"],stage["max_words"]


def run_one_agent_step(connection: Connection, *, worker_id: str,
                       executor=analyze_text, lease_seconds: int=120):
    """Claim/read under locks, run registered read-only code OUTSIDE locks."""
    require_idle(connection)
    lease=claim_operation(
        connection,worker_id=worker_id,capabilities=(SLOW_TOOL,),
        operation_types=(PROCEDURE_AGENT_OPERATION,),
        lease_seconds=lease_seconds,
    )
    if lease is None:
        return None
    try:
        with connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                instruction,max_words=_read_authorized_execution(cur,lease)
    except (VersionConflict,PermissionDenied,StaleOperation):
        # No invocation happened; stale authority must never run the tool.
        return lease
    try:
        result=executor(instruction,max_words)
        if not isinstance(result,str) or not 1<=len(result)<=12000:
            raise ValueError("registered tool returned invalid text")
    except Exception as exc:
        try:
            fail_operation(
                connection,lease,reason=f"read-only tool failed: {type(exc).__name__}",
            )
        except (VersionConflict,PermissionDenied,StaleOperation):
            pass
        return lease
    with connection.transaction():
        connection.execute(
            """INSERT INTO procedure_agent_operation_results(
                 operation_id,claim_generation,result_text
               ) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
            (lease.operation_id,lease.claim_generation,result),
        )
    # P0 completion enforces lease and the SAME live procedure/task versions.
    # An orphan attempt result is never accepted by reconciliation.
    try:
        complete_operation(connection,lease,
                           result_ref=_result_ref(lease.operation_id,
                                                  lease.claim_generation))
    except (VersionConflict,PermissionDenied,StaleOperation):
        pass
    return lease


def reconcile_agent_step(connection: Connection, actor: TrustedPlatformActor,
                         operation_id: UUID) -> AgentStepReconcileResult:
    """Promote only P0-fenced results into procedure-owned step evidence.

    Called on worker completion or after restart. No tool call or LLM happens
    under locks. Stale approvals are invalidated without changing steps.
    """
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor:
        raise PermissionDenied("procedure result reconciliation requires platform authority")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM operations WHERE operation_id=%s AND user_id=%s
                   FOR SHARE""",(operation_id,actor.user_id),
            )
            operation=cur.fetchone()
            if operation is None or operation["operation_type"]!=PROCEDURE_AGENT_OPERATION:
                raise PermissionDenied("not an owned procedure agent operation")
            cur.execute(
                """SELECT * FROM procedure_agent_operation_authorizations
                   WHERE operation_id=%s AND user_id=%s AND instance_id=%s
                   FOR UPDATE""",
                (operation_id,actor.user_id,operation["instance_id"]),
            )
            stage=cur.fetchone()
            if stage is None:
                raise PermissionDenied("operation has no approved stage")
            if stage["state"]!="AUTHORIZED":
                cur.execute(
                    """SELECT step_event_id,new_status
                       FROM procedure_run_step_events
                       WHERE event_id=%s""",
                    (stage["result_event_id"],),
                )
                prior=cur.fetchone()
                return AgentStepReconcileResult(
                    operation_id=operation_id,run_id=stage["run_id"],
                    resolution=("INVALIDATED" if stage["state"]=="INVALIDATED"
                                else prior["new_status"] if prior else "INVALIDATED"),
                    step_event_id=prior["step_event_id"] if prior else None,
                    replayed=True,
                )
            # Only the selected persona may accept new procedure state.
            # A fully completed P0 result is retained and may be reconciled
            # after this persona is selected again; no automatic activation.
            cur.execute(
                """SELECT instance_id FROM persona_selection
                   WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if selected is None or selected["instance_id"]!=operation["instance_id"]:
                raise StaleSelection("defer AGENT result until persona is selected")
            if operation["status"] not in (
                "COMPLETED","FAILED","BLOCKED","CANCELLED","OUTCOME_UNKNOWN"
            ):
                raise VersionConflict("operation result not terminal")
            after={
                "COMPLETED":"VERIFIED","FAILED":"FAILED",
                "BLOCKED":"BLOCKED","CANCELLED":"BLOCKED",
                "OUTCOME_UNKNOWN":"OUTCOME_UNKNOWN",
            }[operation["status"]]
            result_ref=None
            if after=="VERIFIED":
                result_ref=_result_ref(operation_id,operation["claim_generation"])
                cur.execute(
                    """SELECT 1 FROM procedure_agent_operation_results
                       WHERE operation_id=%s AND claim_generation=%s
                         AND length(result_text) BETWEEN 1 AND 12000""",
                    (operation_id,operation["claim_generation"]),
                )
                if (operation["effect_status"]!="CONFIRMED"
                    or operation["result_ref"]!=result_ref
                    or cur.fetchone() is None):
                    raise VersionConflict("completed operation lacks fenced trusted result")
            # Lock order must match admission/claim: operation -> stage ->
            # persona selection -> graph -> RUN -> step/task. The shared
            # gate reads the run FOR SHARE; taking an exclusive run lock
            # afterwards would make two different completed AGENT steps
            # deadlock while upgrading their shared locks. Serialize here
            # before either reconciliation enters the shared gate.
            _graph_read_lock(cur,actor,operation["instance_id"])
            cur.execute(
                """SELECT * FROM procedure_runs
                   WHERE run_id=%s AND user_id=%s AND instance_id=%s
                   FOR UPDATE""",
                (stage["run_id"],actor.user_id,operation["instance_id"]),
            )
            run=cur.fetchone()
            if run is None:
                raise PermissionDenied("procedure run outside operation scope")
            try:
                # P0 already accepted completion under its live deadline
                # fence. Late reconciliation is evidence admission, not
                # permission to execute a new operation after the deadline.
                # Its FOR SHARE run check now observes the exclusive lock
                # already owned by this transaction.
                require_procedure_agent_gate(
                    cur,operation,allow_past_deadline_evidence=True,
                    allow_completed_background_evidence=True,
                )
            except (VersionConflict,PermissionDenied):
                event=_publish(
                    cur,actor,operation["instance_id"],
                    "procedure.agent.result_invalidated.v1",{
                        "operation_id":str(operation_id),
                        "run_id":str(stage["run_id"]),
                        "reason":"Task, run or step execution grant is no longer current",
                    },uuid4(),trust_level="TRUSTED_PLATFORM",
                )
                cur.execute(
                    """UPDATE procedure_agent_operation_authorizations
                       SET state='INVALIDATED',result_event_id=%s
                       WHERE operation_id=%s""",(event,operation_id),
                )
                return AgentStepReconcileResult(
                    operation_id=operation_id,run_id=stage["run_id"],
                    resolution="INVALIDATED",
                )
            cur.execute(
                """SELECT * FROM procedure_run_steps
                   WHERE run_id=%s AND step_id=%s FOR UPDATE""",
                (stage["run_id"],stage["step_id"]),
            )
            step=cur.fetchone()
            next_run=run["version"]+1
            next_step=step["version"]+1
            step_event_id=uuid4()
            event=_publish(
                cur,actor,operation["instance_id"],
                "procedure.agent.result_accepted.v1",{
                    "operation_id":str(operation_id),
                    "run_id":str(stage["run_id"]),
                    "step_id":stage["step_id"],
                    "step_event_id":str(step_event_id),
                    "claim_generation":operation["claim_generation"],
                    "result_ref":result_ref,
                    "status":after,
                    "task_completion_changed":False,
                },uuid4(),trust_level="TRUSTED_PLATFORM",
            )
            cur.execute(
                """INSERT INTO procedure_run_step_events(
                     step_event_id,run_id,step_id,user_id,instance_id,
                     run_version,step_version,kind,old_status,new_status,
                     detail,source_conversation_id,source_turn_id,source_quote,
                     approval_turn_id,event_id
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (step_event_id,stage["run_id"],stage["step_id"],actor.user_id,
                 operation["instance_id"],next_run,next_step,after,
                 step["status"],after,None,stage["source_conversation_id"],
                 stage["source_turn_id"],stage["source_quote"],
                 stage["approval_turn_id"],event),
            )
            cur.execute(
                """UPDATE procedure_run_steps SET status=%s,version=%s,
                     last_step_event_id=%s WHERE run_id=%s AND step_id=%s""",
                (after,next_step,step_event_id,stage["run_id"],stage["step_id"]),
            )
            cur.execute(
                """UPDATE procedure_runs SET version=%s,
                     updated_at=clock_timestamp() WHERE run_id=%s""",
                (next_run,stage["run_id"]),
            )
            cur.execute(
                """UPDATE procedure_agent_operation_authorizations
                   SET state='RECONCILED',result_event_id=%s WHERE operation_id=%s""",
                (event,operation_id),
            )
            return AgentStepReconcileResult(
                operation_id=operation_id,run_id=stage["run_id"],
                resolution=after,step_event_id=step_event_id,
            )
