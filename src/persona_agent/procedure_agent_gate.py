"""Shared P3-04 task/run/step execution gate for the EXISTING P0 scheduler.

All checks are deterministic, bounded, and run under short DB transactions.
Only the registered read-only text.statistics capability is admitted.
"""
from datetime import datetime, timezone
from uuid import UUID

from .contracts import TrustedActor
from .runtime import PermissionDenied, VersionConflict
from .task_state import _active_goal_chain, _confirmation_state, _graph_read_lock
from .tool_contracts import SLOW_TOOL, PROCEDURE_AGENT_OPERATION


def require_procedure_agent_gate(cur, operation: dict, *,
                                 allow_past_deadline_evidence: bool=False,
                                 allow_completed_background_evidence: bool=False,
                                 stage_only: bool=False) -> dict:
    """Return approved staging record or reject a stale task/run/capability.

    Caller holds selected persona authority (FOR SHARE) and, when applicable,
    the P0 operation row lock. Stage, Task and Run versions are checked again
    at enqueue, claim, before the read-only tool and at completion.
    """
    if operation["operation_type"] != PROCEDURE_AGENT_OPERATION:
        raise PermissionDenied("not a registered procedure agent operation")
    user_id=operation["user_id"]
    instance_id=operation["instance_id"]
    operation_id=operation["operation_id"]
    cur.execute(
        """SELECT * FROM procedure_agent_operation_authorizations
           WHERE operation_id=%s AND user_id=%s AND instance_id=%s
             AND state='AUTHORIZED' FOR SHARE""",
        (operation_id,user_id,instance_id),
    )
    stage=cur.fetchone()
    if stage is None:
        raise PermissionDenied("procedure operation missing current approved authorization")
    if not stage_only:
        background=(operation.get("execution_policy")=="BACKGROUND_RESUMABLE")
        if background:
            if (operation.get("origin")!="GOAL"
                or operation.get("priority_class")!="P2"
                or operation.get("effect_class")!="READ_ONLY"
                or stage["approved_background_epoch"] is None):
                raise PermissionDenied("Background AGENT operation lacks user-approved P2 grant")
            historical_result=(
                allow_completed_background_evidence
                and operation.get("status")=="COMPLETED"
                and operation.get("effect_status")=="CONFIRMED"
                and stage["approved_background_epoch"]
                    ==operation.get("background_epoch")
            )
            if not historical_result:
                cur.execute(
                    """SELECT instance_id,generation,mode,background_epoch
                       FROM persona_selection WHERE user_id=%s FOR SHARE""",(user_id,),
                )
                selected=cur.fetchone()
                if (selected is None or selected["instance_id"]!=instance_id
                    or selected["mode"]!="BACKGROUND"
                    or stage["approved_background_epoch"]!=selected["background_epoch"]
                    or operation.get("background_epoch")!=selected["background_epoch"]):
                    raise VersionConflict("Background approval epoch no longer current")
                # Completion evidence was accepted under P0's live epoch
                # fence. Reconciliation is only a historical evidence write,
                # not permission to dispatch another Background tool action.
                from .operation_scheduler import _eligible_for_policy
                if not _eligible_for_policy(cur,operation,selected):
                    raise VersionConflict("Background task alignment revoked")
        elif stage["approved_background_epoch"] is not None:
            raise PermissionDenied("Background-only step grant cannot become P1 authority")
    expected={
        "task":stage["task_version"],
        "procedure_run":stage["run_version"],
        "procedure_step":stage["step_version"],
    }
    if (operation["task_id"]!=stage["task_id"]
        or operation["input_versions"]!=expected
        or operation["selection_generation"]!=stage["selection_generation"]
        or operation["operation_type"]!=PROCEDURE_AGENT_OPERATION):
        raise PermissionDenied("procedure operation does not match the approved task/run/step versions")

    _graph_read_lock(cur,TrustedActor(user_id=user_id,source_id="procedure.gate"),
                     instance_id)
    cur.execute(
        """SELECT * FROM procedure_runs WHERE run_id=%s
           AND user_id=%s AND instance_id=%s FOR SHARE""",
        (stage["run_id"],user_id,instance_id),
    )
    run=cur.fetchone()
    if (run is None or run["status"]!="ACTIVE"
        or run["version"]!=stage["run_version"]
        or run["task_id"]!=stage["task_id"]):
        raise VersionConflict("procedure run authority changed or run is inactive")
    cur.execute(
        """SELECT * FROM procedure_run_steps WHERE run_id=%s AND step_id=%s
           AND user_id=%s AND instance_id=%s FOR SHARE""",
        (stage["run_id"],stage["step_id"],user_id,instance_id),
    )
    step=cur.fetchone()
    if (step is None or step["executor"]!="AGENT"
        or step["capability"]!=SLOW_TOOL
        or step["version"]!=stage["step_version"]
        or step["status"] not in ("NOT_STARTED","FAILED","BLOCKED")):
        raise VersionConflict("AGENT step changed or is not pending execution")
    cur.execute(
        """SELECT * FROM goal_tasks WHERE task_id=%s
           AND user_id=%s AND instance_id=%s FOR SHARE""",
        (stage["task_id"],user_id,instance_id),
    )
    task=cur.fetchone()
    if (task is None or task["executor"]!="AGENT"
        or task["capability"]!=SLOW_TOOL
        or task["status"]!="PLANNED"
        or task["version"]!=stage["task_version"]
        or task["suspended"]):
        raise VersionConflict("task authority, capability or status changed")
    if (not allow_past_deadline_evidence
        and task["deadline"] is not None
        and task["deadline"]<=datetime.now(timezone.utc)):
        raise VersionConflict("task deadline expired")
    actor=TrustedActor(user_id=user_id,source_id="procedure.gate")
    _active_goal_chain(cur,actor,instance_id,task["goal_id"])
    cur.execute(
        """SELECT 1 FROM goal_tasks WHERE user_id=%s AND instance_id=%s
             AND exclusive_only AND task_id<>%s LIMIT 1""",
        (user_id,instance_id,stage["task_id"]),
    )
    if cur.fetchone() is not None:
        raise VersionConflict("another task has exclusive priority")
    unmet,_,_,_=_confirmation_state(cur,actor,instance_id,[stage["task_id"]])
    if unmet.get(stage["task_id"]):
        raise VersionConflict("AGENT task dependency no longer effective")
    if background:
        # The new grant must carry valid source lineage at EVERY P0 fence;
        # the predecessor's old epoch is never an execution permission.
        from .procedure_agent_checkpoint import require_resume_lineage
        require_resume_lineage(cur,operation,step)
    return stage
