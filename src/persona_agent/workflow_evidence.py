"""Read existing immutable Task/Procedure evidence through the Memory owner.

Status and receipts remain historical facts. Eligibility is not an implicit
correction, execution grant, or change to a fixed/shared method.
"""
from .runtime import VersionConflict

_FUNCTIONS={
    "task_plan":"workflow_task_plan_current",
    "task_report":"workflow_task_report_current",
    "task_confirmation":"workflow_task_confirmation_current",
    "step_event":"workflow_step_event_current",
    "evaluation":"workflow_evaluation_current",
    "proposal":"workflow_proposal_current",
    "variant":"workflow_variant_current",
    "run":"workflow_run_current",
    "agent_grant":"workflow_agent_grant_current",
}


def current(cur,user_id,instance_id,kind,*ids):
    function=_FUNCTIONS[kind]
    placeholders=",".join(["%s"]*(2+len(ids)))
    cur.execute(f"SELECT {function}({placeholders}) AS eligible",
                (user_id,instance_id,*ids))
    return bool(cur.fetchone()["eligible"])


def lock_scope(cur,user_id,instance_id):
    """Late shared fence against deferred Memory-owner commit triggers.

    Caller already owns domain/graph/operation locks. No owner writes that
    require Memory locks or model/tool calls may follow this lock.
    """
    cur.execute(
        """SELECT revision FROM historical_recall_revisions
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise VersionConflict("workflow evidence scope unavailable")


def require_current(cur,user_id,instance_id,kind,*ids):
    if not current(cur,user_id,instance_id,kind,*ids):
        raise VersionConflict(f"{kind} source evidence requires reassessment")


def require_command_source(cur,actor,command):
    cur.execute(
        "SELECT workflow_source_current(%s,%s,%s,%s,%s) AS eligible",
        (actor.user_id,command.instance_id,command.conversation_id,
         command.evidence_turn_id,command.approval_turn_id),
    )
    if not cur.fetchone()["eligible"]:
        raise VersionConflict("workflow command source evidence requires reassessment")
