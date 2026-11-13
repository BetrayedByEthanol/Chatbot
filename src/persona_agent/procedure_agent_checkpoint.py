"""P4-05b: validated safe-stop evidence for one registered read-only AGENT step.

Checkpoint bytes are immutable, source-scoped and derived from authoritative
Procedure inputs. Loading a checkpoint NEVER queues or executes an operation.
Renewed Background authorization and actual resumed execution are P4-05c.
"""
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .operation_contracts import (
    InvalidOperation, require_idle,
)
from .procedure_agent_gate import require_procedure_agent_gate
from .runtime import PermissionDenied, StaleSelection, VersionConflict
from .tool_contracts import PROCEDURE_AGENT_OPERATION, SLOW_TOOL


class RestoredProcedureAgentCheckpoint(BaseModel):
    """Validated, read-only recovery input: NOT an execution grant."""

    model_config=ConfigDict(frozen=True,extra="forbid")
    operation_id: UUID
    checkpoint_ref: str
    claim_generation: int
    source_background_epoch: int
    task_id: UUID
    run_id: UUID
    step_id: str
    instruction: str
    max_words: int
    restart_required: bool = True


def _checkpoint_ref(operation_id: UUID, generation: int) -> str:
    return f"procedure-step:v1:{operation_id}:{generation}"


def write_safe_stop_checkpoint(cur, operation: dict) -> str | None:
    """Internal P0 hook, called ONLY within fenced suspension ACK transaction.

    Uses immutable grant pins and current Procedure instruction, without an
    epoch-current gate: the Foreground transition has *already* revoked P2.
    A changed/missing owner input is not a safe checkpoint. Do not block ACK.
    """
    if (operation["operation_type"]!=PROCEDURE_AGENT_OPERATION
        or operation["origin"]!="GOAL" or operation["priority_class"]!="P2"
        or operation["execution_policy"]!="BACKGROUND_RESUMABLE"
        or operation["effect_class"]!="READ_ONLY"
        or operation["effect_status"]!="PREPARED"
        or operation["status"]!="SUSPEND_REQUESTED"
        or operation["background_epoch"] is None
        or operation["claim_generation"]<1):
        return None
    cur.execute(
        """SELECT a.*,s.instruction,s.version AS live_step_version,
                  s.status AS live_step_status,
                  s.capability AS live_capability,
                  s.executor AS live_executor
           FROM procedure_agent_operation_authorizations a
           JOIN procedure_run_steps s
             ON s.run_id=a.run_id AND s.step_id=a.step_id
            AND s.user_id=a.user_id AND s.instance_id=a.instance_id
           WHERE a.operation_id=%s AND a.user_id=%s
             AND a.instance_id=%s""",
        (operation["operation_id"],operation["user_id"],operation["instance_id"]),
    )
    stage=cur.fetchone()
    if stage is None:
        return None
    expected={
        "task":stage["task_version"],
        "procedure_run":stage["run_version"],
        "procedure_step":stage["step_version"],
    }
    if (stage["state"]!="AUTHORIZED"
        or stage["approved_background_epoch"]!=operation["background_epoch"]
        or stage["selection_generation"]!=operation["selection_generation"]
        or stage["task_id"]!=operation["task_id"]
        or operation["input_versions"]!=expected
        or stage["capability"]!=SLOW_TOOL
        or stage["live_capability"]!=SLOW_TOOL
        or stage["live_executor"]!="AGENT"
        or stage["live_step_version"]!=stage["step_version"]
        or stage["live_step_status"] not in ("NOT_STARTED","FAILED","BLOCKED")
        or not isinstance(stage["instruction"],str)):
        return None
    reference=_checkpoint_ref(operation["operation_id"],operation["claim_generation"])
    digest=sha256(stage["instruction"].encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO procedure_agent_checkpoints(
             operation_id,claim_generation,user_id,instance_id,
             task_id,run_id,step_id,task_version,run_version,step_version,
             selection_generation,background_epoch,capability,max_words,
             schema_version,boundary,instruction_sha256,checkpoint_ref
           ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,
                    'READ_ONLY_RESTART_REQUIRED',%s,%s)""",
        (operation["operation_id"],operation["claim_generation"],
         operation["user_id"],operation["instance_id"],stage["task_id"],
         stage["run_id"],stage["step_id"],stage["task_version"],
         stage["run_version"],stage["step_version"],
         stage["selection_generation"],operation["background_epoch"],
         SLOW_TOOL,stage["max_words"],digest,reference),
    )
    return reference


def load_validated_procedure_checkpoint(
    connection: Connection, actor: TrustedActor, *, operation_id: UUID,
) -> RestoredProcedureAgentCheckpoint:
    """Restore only immutable input evidence after user returns.

    Checks persona selection, full Goal/Task/Run/Step owner constraints, Task
    deadline, step instruction digest and source claim identity. Does NOT
    reauthorize the expired Background epoch or return a P0 execution lease.
    """
    require_idle(connection)
    if type(actor) is not TrustedActor:
        raise PermissionDenied("checkpoint restoration requires primary user")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM operations
                   WHERE operation_id=%s AND user_id=%s FOR SHARE""",
                (operation_id,actor.user_id),
            )
            operation=cur.fetchone()
            if operation is None:
                raise PermissionDenied("checkpoint not owned by authenticated user")
            if (operation["operation_type"]!=PROCEDURE_AGENT_OPERATION
                or operation["priority_class"]!="P2"
                or operation["execution_policy"]!="BACKGROUND_RESUMABLE"
                or operation["effect_class"]!="READ_ONLY"
                or operation["status"]!="SUSPENDED"
                or operation["effect_status"]!="PREPARED"):
                raise InvalidOperation("operation has no safe suspended read-only state")
            cur.execute(
                """SELECT * FROM procedure_agent_checkpoints
                   WHERE operation_id=%s AND claim_generation=%s
                     AND user_id=%s AND instance_id=%s""",
                (operation_id,operation["claim_generation"],
                 actor.user_id,operation["instance_id"]),
            )
            saved=cur.fetchone()
            if saved is None or saved["checkpoint_ref"]!=operation["checkpoint_ref"]:
                raise VersionConflict("operation has no valid immutable checkpoint")
            if (saved["schema_version"]!=1
                or saved["boundary"]!="READ_ONLY_RESTART_REQUIRED"
                or saved["capability"]!=SLOW_TOOL
                or saved["background_epoch"]!=operation["background_epoch"]
                or saved["selection_generation"]!=operation["selection_generation"]
                or saved["task_id"]!=operation["task_id"]
                or saved["checkpoint_ref"]!=_checkpoint_ref(operation_id,saved["claim_generation"])
                or operation["input_versions"]!={
                    "task":saved["task_version"],
                    "procedure_run":saved["run_version"],
                    "procedure_step":saved["step_version"],
                }):
                raise VersionConflict("checkpoint metadata does not match P0 claim")
            cur.execute(
                """SELECT instance_id,generation,mode FROM persona_selection
                   WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if (selected is None or selected["instance_id"]!=operation["instance_id"]
                or selected["generation"]!=saved["selection_generation"]
                or selected["mode"]!="FOREGROUND"):
                raise StaleSelection("select the same persona to restore checkpoint")
            # Full owner gate is intentionally stage_only: old Background
            # authority is NOT reissued by inspecting recovery data.
            stage=require_procedure_agent_gate(cur,operation,stage_only=True)
            if (stage["run_id"]!=saved["run_id"]
                or stage["step_id"]!=saved["step_id"]
                or stage["task_id"]!=saved["task_id"]
                or stage["max_words"]!=saved["max_words"]
                or stage["capability"]!=SLOW_TOOL):
                raise VersionConflict("procedure checkpoint owner metadata changed")
            cur.execute(
                """SELECT instruction FROM procedure_run_steps
                   WHERE run_id=%s AND step_id=%s
                     AND user_id=%s AND instance_id=%s""",
                (saved["run_id"],saved["step_id"],
                 actor.user_id,operation["instance_id"]),
            )
            step=cur.fetchone()
            if (step is None or not isinstance(step["instruction"],str)
                or sha256(step["instruction"].encode("utf-8")).hexdigest()
                   != saved["instruction_sha256"]):
                raise VersionConflict("procedure instruction differs from checkpoint")
            cur.execute(
                """SELECT (deadline IS NULL OR deadline>clock_timestamp()) AS eligible
                   FROM goal_tasks WHERE task_id=%s AND user_id=%s
                     AND instance_id=%s""",
                (saved["task_id"],actor.user_id,operation["instance_id"]),
            )
            due=cur.fetchone()
            if due is None or not due["eligible"]:
                raise VersionConflict("task deadline expired before restoration")
            return RestoredProcedureAgentCheckpoint(
                operation_id=operation_id,
                checkpoint_ref=saved["checkpoint_ref"],
                claim_generation=saved["claim_generation"],
                source_background_epoch=saved["background_epoch"],
                task_id=saved["task_id"],
                run_id=saved["run_id"],
                step_id=saved["step_id"],
                instruction=step["instruction"],
                max_words=saved["max_words"],
            )


def require_renewable_source(cur, actor: TrustedActor, source: dict,
                             command) -> None:
    """Atomic Procedure-owner validation before superseding a P2 grant.

    The old operation is SUSPENDED and its original approval is still
    AUTHORIZED. This does not confer new execution authority by itself.
    """
    if (source["operation_id"]!=command.resume_from_operation_id
        or source["operation_id"]==command.operation_id
        or source["user_id"]!=actor.user_id
        or source["instance_id"]!=command.instance_id
        or source["task_id"] is None
        or source["operation_type"]!=PROCEDURE_AGENT_OPERATION
        or source["origin"]!="GOAL"
        or source["priority_class"]!="P2"
        or source["execution_policy"]!="BACKGROUND_RESUMABLE"
        or source["effect_class"]!="READ_ONLY"
        or source["status"]!="SUSPENDED"
        or source["effect_status"]!="PREPARED"
        or source["claim_generation"]<1
        or source["background_epoch"] is None
        or source["background_epoch"]>=command.expected_background_epoch
        or source["checkpoint_ref"]!=command.resume_checkpoint_ref):
        raise VersionConflict("source is not a safely suspended approved step")
    # This checks the original Procedure authorization and the *current*
    # Goal/Task/Run/Step domain versions, without reviving its old epoch.
    source_stage=require_procedure_agent_gate(cur,source,stage_only=True)
    if (source_stage["operation_id"]!=source["operation_id"]
        or source_stage["approved_background_epoch"]!=source["background_epoch"]
        or source_stage["run_id"]!=command.run_id
        or source_stage["step_id"]!=command.step_id
        or source_stage["task_id"]!=source["task_id"]
        or source_stage["max_words"]!=command.max_words
        or source_stage["run_version"]!=command.expected_run_version
        or source_stage["step_version"]!=command.expected_step_version
        or source_stage["task_version"]!=command.expected_task_version
        or source["input_versions"]!={
            "task":command.expected_task_version,
            "procedure_run":command.expected_run_version,
            "procedure_step":command.expected_step_version,
        }):
        raise VersionConflict("renewal differs from source owner version pins")
    cur.execute(
        """SELECT * FROM procedure_agent_checkpoints
           WHERE checkpoint_ref=%s AND operation_id=%s
             AND claim_generation=%s AND user_id=%s AND instance_id=%s""",
        (command.resume_checkpoint_ref,source["operation_id"],
         source["claim_generation"],actor.user_id,command.instance_id),
    )
    saved=cur.fetchone()
    if (saved is None
        or saved["schema_version"]!=1
        or saved["boundary"]!="READ_ONLY_RESTART_REQUIRED"
        or saved["capability"]!=SLOW_TOOL
        or saved["background_epoch"]!=source["background_epoch"]
        or saved["selection_generation"]!=source["selection_generation"]
        or saved["checkpoint_ref"]!=_checkpoint_ref(
            source["operation_id"],source["claim_generation"])
        or saved["run_id"]!=command.run_id
        or saved["step_id"]!=command.step_id
        or saved["task_id"]!=source["task_id"]
        or saved["task_version"]!=command.expected_task_version
        or saved["run_version"]!=command.expected_run_version
        or saved["step_version"]!=command.expected_step_version
        or saved["max_words"]!=command.max_words):
        raise VersionConflict("source checkpoint is missing, stale or incompatible")
    cur.execute(
        """SELECT instruction FROM procedure_run_steps
           WHERE run_id=%s AND step_id=%s AND user_id=%s AND instance_id=%s""",
        (saved["run_id"],saved["step_id"],actor.user_id,command.instance_id),
    )
    step=cur.fetchone()
    if (step is None or not isinstance(step["instruction"],str)
        or sha256(step["instruction"].encode("utf-8")).hexdigest()
           !=saved["instruction_sha256"]):
        raise VersionConflict("source Procedure input checksum changed")
    cur.execute(
        """SELECT (deadline IS NULL OR deadline>clock_timestamp()) AS eligible
           FROM goal_tasks WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
        (saved["task_id"],actor.user_id,command.instance_id),
    )
    deadline=cur.fetchone()
    if deadline is None or not deadline["eligible"]:
        raise VersionConflict("source Task deadline expired")


def require_resume_lineage(cur, operation: dict, step: dict) -> None:
    """Verify an immutable source at P0 enqueue/claim/dispatch/completion.

    Called by the Procedure gate for an authorized *new* operation; a prior
    checkpoint is only evidence, not permission for the current epoch.
    """
    cur.execute(
        """SELECT * FROM procedure_agent_resume_links
           WHERE operation_id=%s AND user_id=%s AND instance_id=%s""",
        (operation["operation_id"],operation["user_id"],operation["instance_id"]),
    )
    link=cur.fetchone()
    if link is None:
        return
    if (operation.get("origin")!="GOAL"
        or operation.get("priority_class")!="P2"
        or operation.get("execution_policy")!="BACKGROUND_RESUMABLE"
        or operation.get("effect_class")!="READ_ONLY"
        or operation.get("background_epoch")!=link["approved_background_epoch"]):
        # During P0 enqueue the new operation's epoch is already selected.
        raise PermissionDenied("resumed step cannot bypass Background P2 admission")
    cur.execute(
        """SELECT * FROM operations WHERE operation_id=%s FOR SHARE""",
        (link["source_operation_id"],),
    )
    source=cur.fetchone()
    if (source is None or source["user_id"]!=operation["user_id"]
        or source["instance_id"]!=operation["instance_id"]
        or source["task_id"]!=operation["task_id"]
        or source["operation_type"]!=PROCEDURE_AGENT_OPERATION
        or source["status"]!="SUSPENDED"
        or source["effect_status"]!="PREPARED"
        or source["checkpoint_ref"]!=link["checkpoint_ref"]
        or source["background_epoch"] is None
        or source["background_epoch"]>=link["approved_background_epoch"]):
        raise VersionConflict("resumption source was invalidated")
    cur.execute(
        """SELECT * FROM procedure_agent_checkpoints
           WHERE checkpoint_ref=%s AND operation_id=%s
             AND claim_generation=%s AND user_id=%s AND instance_id=%s""",
        (link["checkpoint_ref"],source["operation_id"],source["claim_generation"],
         operation["user_id"],operation["instance_id"]),
    )
    saved=cur.fetchone()
    if (saved is None or saved["schema_version"]!=1
        or saved["boundary"]!="READ_ONLY_RESTART_REQUIRED"
        or saved["capability"]!=SLOW_TOOL
        or saved["background_epoch"]!=source["background_epoch"]
        or saved["selection_generation"]!=source["selection_generation"]
        or saved["checkpoint_ref"]!=_checkpoint_ref(
            source["operation_id"],source["claim_generation"])
        or saved["task_id"]!=operation["task_id"]
        or saved["run_id"]!=step["run_id"]
        or saved["step_id"]!=step["step_id"]
        or saved["task_version"]!=operation["input_versions"]["task"]
        or saved["run_version"]!=operation["input_versions"]["procedure_run"]
        or saved["step_version"]!=operation["input_versions"]["procedure_step"]
        or sha256(step["instruction"].encode("utf-8")).hexdigest()
            !=saved["instruction_sha256"]):
        raise VersionConflict("resumption checkpoint source no longer matches")
