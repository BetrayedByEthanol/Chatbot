"""P3-04 durable ProcedureRun state and step evidence.

The Procedure Manager owns run progress; Conversation owns thread attention,
Task owns completion, and P0 owns operations. No operation dispatch here.
"""
import hashlib
import json
from uuid import UUID, uuid4
from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .goal_state import _read_selected
from .operation_contracts import require_idle
from .procedure_contracts import ProcedureStep, validate_variant
from .procedure_state import (
    _command, _current_persona, _definition, _publish, _selected_strategy,
)
from .procedure_run_contracts import (
    AcknowledgeProcedureInstructionCommand, CorrectProcedureStepCommand,
    LinkProcedureThreadCommand, ProcedureRunResult, ProcedureRunStepView,
    ProcedureRunThreadLink, ProcedureRunView, RecordProcedureStepCommand,
    SetProcedureRunStatusCommand, StartProcedureRunCommand,
)
from .runtime import PermissionDenied, VersionConflict
from .task_state import (
    _active_goal_chain, _confirmation_state, _graph_read_lock, _task,
)


def _run(cur, actor: TrustedActor, instance_id: UUID, run_id: UUID,
         *, lock: bool = True) -> dict:
    suffix="FOR UPDATE" if lock else "FOR SHARE"
    cur.execute(
        f"""SELECT * FROM procedure_runs WHERE run_id=%s
            AND user_id=%s AND instance_id=%s {suffix}""",
        (run_id,actor.user_id,instance_id),
    )
    run=cur.fetchone()
    if run is None:
        raise PermissionDenied("procedure run outside selected user/persona")
    return run


def _step(cur, run_id: UUID, step_id: str, *, lock: bool = True) -> dict:
    suffix="FOR UPDATE" if lock else "FOR SHARE"
    cur.execute(
        f"""SELECT * FROM procedure_run_steps
            WHERE run_id=%s AND step_id=%s {suffix}""",
        (run_id,step_id),
    )
    step=cur.fetchone()
    if step is None:
        raise PermissionDenied("step is not in the pinned procedure run")
    return step


def _expected(run: dict, expected: int) -> None:
    if run["version"]!=expected:
        raise VersionConflict("procedure run version changed")


def _evidence_step_event(cur, actor: TrustedActor, cmd, run: dict,
                         step: dict, receipt: UUID, *, kind: str, after: str,
                         detail: str | None = None, response_id: UUID | None = None,
                         corrected_event_id: UUID | None = None
                         ) -> ProcedureRunResult:
    event_uuid=uuid4()
    next_run=run["version"]+1
    next_step=step["version"]+1
    event=_publish(cur,actor,cmd.instance_id,"procedure.run.step.v1",{
        "run_id":str(run["run_id"]),"step_id":step["step_id"],
        "step_event_id":str(event_uuid),"transition":kind,
        "from":step["status"],"to":after,
        "run_version":next_run,"step_version":next_step,
        "response_id":str(response_id) if response_id else None,
        "corrected_step_event_id":str(corrected_event_id) if corrected_event_id else None,
        "source_turn_id":str(cmd.evidence_turn_id),
        "approval_turn_id":str(cmd.approval_turn_id) if cmd.approval_turn_id else None,
    },receipt)
    cur.execute(
        """INSERT INTO procedure_run_step_events(
             step_event_id,run_id,step_id,user_id,instance_id,run_version,
             step_version,kind,old_status,new_status,detail,
             corrected_step_event_id,source_conversation_id,source_turn_id,
             source_quote,approval_turn_id,response_id,event_id
           ) VALUES(
             %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
           )""",
        (event_uuid,run["run_id"],step["step_id"],actor.user_id,
         cmd.instance_id,next_run,next_step,kind,step["status"],after,
         detail,corrected_event_id,cmd.conversation_id,cmd.evidence_turn_id,
         cmd.evidence_quote,cmd.approval_turn_id,response_id,event),
    )
    cur.execute(
        """UPDATE procedure_run_steps SET status=%s,version=%s,
             last_step_event_id=%s
           WHERE run_id=%s AND step_id=%s""",
        (after,next_step,event_uuid,run["run_id"],step["step_id"]),
    )
    # Corrections reopen a finished run. A different step's progress must
    # NOT clear the review gate while any step still needs reassessment.
    status=run["status"]
    if kind=="CORRECTED":
        status="NEEDS_REVIEW"
    elif run["status"]=="NEEDS_REVIEW":
        cur.execute(
            """SELECT EXISTS(
                 SELECT 1 FROM procedure_run_steps
                 WHERE run_id=%s AND status='NEEDS_REVIEW'
               ) AS unresolved""",
            (run["run_id"],),
        )
        if not cur.fetchone()["unresolved"]:
            status="ACTIVE"
    cur.execute(
        """UPDATE procedure_runs SET version=%s,status=%s,
             updated_at=clock_timestamp() WHERE run_id=%s""",
        (next_run,status,run["run_id"]),
    )
    action=("STEP_CORRECTED" if kind=="CORRECTED"
            else "INSTRUCTION_DELIVERED" if kind=="INSTRUCTION_DELIVERED"
            else "STEP_RECORDED")
    return ProcedureRunResult(
        run_id=run["run_id"],version=next_run,event_id=event,action=action,
        step_id=step["step_id"],step_version=next_step,step_event_id=event_uuid,
    )


def _step_admission(cur, actor, cmd, *, allow_finished: bool=False):
    run=_run(cur,actor,cmd.instance_id,cmd.run_id)
    _expected(run,cmd.expected_run_version)
    if run["status"]=="CANCELLED" or (
        run["status"]=="USER_CONFIRMED" and not allow_finished
    ):
        raise VersionConflict("procedure run is terminal")
    if run["status"]=="PAUSED":
        raise VersionConflict("resume procedure before recording new step progress")
    step=_step(cur,cmd.run_id,cmd.step_id)
    if step["version"]!=cmd.expected_step_version:
        raise VersionConflict("procedure step version changed")
    return run,step


def start_procedure_run(connection: Connection, actor: TrustedActor,
                        command: StartProcedureRunCommand) -> ProcedureRunResult:
    """Explicitly track a pinned method; never select/execute a tool."""
    def apply(cur,receipt):
        persona=_current_persona(cur,actor,command.instance_id)
        definition=_definition(cur,command.template_id,command.template_version,persona)
        if command.variant_id is not None:
            cur.execute(
                """SELECT p.template_id,p.persona_id,p.goal_id,p.strategy_id,
                          r.template_version,r.parameters,r.step_text_overrides,
                          r.adaptation_reason
                   FROM personalized_procedures p
                   JOIN personalized_procedure_revisions r
                     ON r.variant_id=p.variant_id
                   WHERE p.variant_id=%s AND r.revision=%s
                     AND p.user_id=%s AND p.instance_id=%s""",
                (command.variant_id,command.variant_revision,actor.user_id,
                 command.instance_id),
            )
            pinned=cur.fetchone()
            if (pinned is None or pinned["template_id"]!=command.template_id
                or pinned["persona_id"]!=persona
                or pinned["template_version"]!=command.template_version):
                raise PermissionDenied("requested private revision does not match run pin")
            _selected_strategy(cur,actor,command.instance_id,
                               pinned["goal_id"],pinned["strategy_id"])
            steps=validate_variant(
                definition,pinned["parameters"],pinned["step_text_overrides"],
                pinned["adaptation_reason"],
            )
        else:
            steps=validate_variant(definition,command.parameters,{},None)
        if command.task_id is not None:
            # A tracked method is allowed only for an actionable USER task.
            # This does not authorize any AGENT tool execution.
            _graph_read_lock(cur,actor,command.instance_id)
            task=_task(cur,actor,command.instance_id,command.task_id,lock="SHARE")
            _active_goal_chain(cur,actor,command.instance_id,task["goal_id"])
            if (task["status"]!="PLANNED" or task["suspended"]
                or task["executor"]!="USER"):
                raise VersionConflict("task is not actionable for guided run")
            cur.execute(
                """SELECT task_id FROM goal_tasks
                   WHERE user_id=%s AND instance_id=%s AND exclusive_only
                     AND task_id<>%s""",
                (actor.user_id,command.instance_id,command.task_id),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("another task has exclusive priority")
            unmet,_,_,_=_confirmation_state(
                cur,actor,command.instance_id,[command.task_id],
            )
            if unmet.get(command.task_id):
                raise VersionConflict("task dependencies not satisfied")
        encoded=json.dumps([s.model_dump(mode="json") for s in steps],
                           sort_keys=True,separators=(",",":"))
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        event=_publish(cur,actor,command.instance_id,"procedure.run.started.v1",{
            "run_id":str(command.run_id),"template_id":str(command.template_id),
            "template_version":command.template_version,
            "variant_id":str(command.variant_id) if command.variant_id else None,
            "variant_revision":command.variant_revision,
            "task_id":str(command.task_id) if command.task_id else None,
            "steps_sha256":digest,
        },receipt)
        cur.execute(
            """INSERT INTO procedure_runs(
                 run_id,user_id,instance_id,persona_id,template_id,
                 template_version,variant_id,variant_revision,task_id,
                 steps_sha256,source_conversation_id,source_turn_id,
                 approval_turn_id,created_event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.run_id,actor.user_id,command.instance_id,persona,
             command.template_id,command.template_version,command.variant_id,
             command.variant_revision,command.task_id,digest,
             command.conversation_id,command.evidence_turn_id,
             command.approval_turn_id,event),
        )
        for index,step in enumerate(steps,1):
            cur.execute(
                """INSERT INTO procedure_run_steps(
                     run_id,user_id,instance_id,step_id,position,
                     instruction,success_test,executor,capability
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (command.run_id,actor.user_id,command.instance_id,
                 step.step_id,index,step.instruction,step.success_test,
                 step.executor,step.capability),
            )
        return ProcedureRunResult(
            run_id=command.run_id,version=1,event_id=event,action="RUN_STARTED",
        )
    return _command(connection,actor,command,"procedure.run.start.v1",apply,
                    result_type=ProcedureRunResult)


def record_procedure_step(connection: Connection, actor: TrustedActor,
                          command: RecordProcedureStepCommand) -> ProcedureRunResult:
    """User reports and explicit confirmations; never independently VERIFIED."""
    def apply(cur,receipt):
        run,step=_step_admission(cur,actor,command)
        if step["executor"]!="USER":
            raise PermissionDenied("agent steps require future operation-result gate")
        before=step["status"]
        after=command.outcome
        if before==after:
            raise VersionConflict("procedure step progress unchanged")
        if before=="USER_CONFIRMED":
            raise VersionConflict("correct confirmed step before new evidence")
        if after=="USER_CONFIRMED" and before!="USER_REPORTED_DONE":
            raise VersionConflict("step user confirmation needs a current user report")
        if after!="USER_CONFIRMED" and before=="USER_REPORTED_DONE":
            # A contradictory statement must append an explicit correction,
            # rather than quietly overwrite the recorded DONE claim.
            raise VersionConflict("correct the user report before changing it")
        return _evidence_step_event(
            cur,actor,command,run,step,receipt,kind=after,after=after,
            detail=command.detail,
        )
    return _command(
        connection,actor,command,"procedure.run.step.record.v1",apply,
        result_type=ProcedureRunResult,
        requires_approval=(command.outcome=="USER_CONFIRMED"),
    )


def acknowledge_procedure_instruction(connection: Connection, actor: TrustedActor,
                                      command: AcknowledgeProcedureInstructionCommand
                                      ) -> ProcedureRunResult:
    """Only an actually delivered response containing the precise step text.

    Generated or interrupted/unconfirmed speech must never count as heard.
    No user report, success, task state or tool execution is inferred.
    """
    def apply(cur,receipt):
        run,step=_step_admission(cur,actor,command)
        if step["executor"]!="USER":
            raise PermissionDenied("agent instruction cannot be user-delivered")
        cur.execute(
            """SELECT turn_id,response_text,delivery_status FROM conversation_responses
               WHERE response_id=%s AND conversation_id=%s
                 AND user_id=%s AND instance_id=%s""",
            (command.response_id,command.conversation_id,
             actor.user_id,command.instance_id),
        )
        response=cur.fetchone()
        if (response is None or response["turn_id"]!=command.evidence_turn_id
            or response["delivery_status"]!="DELIVERED"):
            raise VersionConflict("instruction response was not acknowledged as delivered")
        if step["instruction"] not in response["response_text"]:
            raise PermissionDenied("delivered response did not contain pinned instruction")
        if step["status"] in ("USER_CONFIRMED","NEEDS_REVIEW"):
            raise VersionConflict("cannot rewrite confirmed or corrected step delivery")
        after=("INSTRUCTED" if step["status"]=="NOT_STARTED" else step["status"])
        return _evidence_step_event(
            cur,actor,command,run,step,receipt,kind="INSTRUCTION_DELIVERED",
            after=after,response_id=command.response_id,
        )
    return _command(
        connection,actor,command,"procedure.run.step.delivery.v1",apply,
        result_type=ProcedureRunResult,requires_approval=False,
    )


def correct_procedure_step(connection: Connection, actor: TrustedActor,
                           command: CorrectProcedureStepCommand) -> ProcedureRunResult:
    """Amend effective progress, preserving every old report/event."""
    def apply(cur,receipt):
        run,step=_step_admission(cur,actor,command,allow_finished=True)
        if step["status"] in ("NOT_STARTED","NEEDS_REVIEW"):
            raise VersionConflict("step has no current claim to correct")
        if step["last_step_event_id"]!=command.superseded_step_event_id:
            raise VersionConflict("step correction must cite the latest evidence")
        return _evidence_step_event(
            cur,actor,command,run,step,receipt,kind="CORRECTED",
            after="NEEDS_REVIEW",detail=command.reason.strip(),
            corrected_event_id=command.superseded_step_event_id,
        )
    return _command(
        connection,actor,command,"procedure.run.step.correct.v1",apply,
        result_type=ProcedureRunResult,
    )


def link_procedure_thread(connection: Connection, actor: TrustedActor,
                          command: LinkProcedureThreadCommand) -> ProcedureRunResult:
    """Link a conversation-owned thread without rewriting its focus or state."""
    def apply(cur,receipt):
        run=_run(cur,actor,command.instance_id,command.run_id)
        _expected(run,command.expected_run_version)
        cur.execute(
            """SELECT thread_id FROM dialogue_threads
               WHERE thread_id=%s AND conversation_id=%s
                 AND user_id=%s AND instance_id=%s""",
            (command.thread_id,command.conversation_id,
             actor.user_id,command.instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("dialogue thread outside selected persona/conversation")
        cur.execute(
            """SELECT 1 FROM procedure_run_thread_links
               WHERE run_id=%s AND conversation_id=%s AND thread_id=%s""",
            (command.run_id,command.conversation_id,command.thread_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("procedure run is already linked to this thread")
        event=_publish(cur,actor,command.instance_id,"procedure.run.thread.linked.v1",{
            "run_id":str(command.run_id),
            "thread_id":str(command.thread_id),
            "conversation_id":str(command.conversation_id),
        },receipt)
        cur.execute(
            """INSERT INTO procedure_run_thread_links(
                 run_id,conversation_id,thread_id,user_id,instance_id,
                 source_turn_id,event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (command.run_id,command.conversation_id,command.thread_id,
             actor.user_id,command.instance_id,
             command.evidence_turn_id,event),
        )
        new=run["version"]+1
        cur.execute(
            """UPDATE procedure_runs SET version=%s,updated_at=clock_timestamp()
               WHERE run_id=%s""",(new,command.run_id),
        )
        return ProcedureRunResult(run_id=command.run_id,version=new,
                                  event_id=event,action="THREAD_LINKED")
    return _command(
        connection,actor,command,"procedure.run.thread.link.v1",apply,
        result_type=ProcedureRunResult,requires_approval=False,
    )


def set_procedure_run_status(connection: Connection, actor: TrustedActor,
                             command: SetProcedureRunStatusCommand
                             ) -> ProcedureRunResult:
    def apply(cur,receipt):
        run=_run(cur,actor,command.instance_id,command.run_id)
        _expected(run,command.expected_run_version)
        old=run["status"]
        statuses={
            "PAUSE":("PAUSED",("ACTIVE","NEEDS_REVIEW")),
            "RESUME":("ACTIVE",("PAUSED",)),
            "CANCEL":("CANCELLED",("ACTIVE","PAUSED","NEEDS_REVIEW")),
            "FINISH":("USER_CONFIRMED",("ACTIVE",)),
        }
        new,eligible=statuses[command.action]
        if old not in eligible:
            raise VersionConflict("invalid procedure run status transition")
        if command.action=="FINISH":
            cur.execute(
                """SELECT status,executor FROM procedure_run_steps WHERE run_id=%s""",
                (command.run_id,),
            )
            steps=cur.fetchall()
            if not steps or any(
                step["executor"]!="USER" or step["status"]!="USER_CONFIRMED"
                for step in steps
            ):
                raise VersionConflict("all user steps require explicit confirmation")
        event=_publish(cur,actor,command.instance_id,"procedure.run.status.v1",{
            "run_id":str(command.run_id),"old_status":old,
            "new_status":new,"action":command.action,
            "task_status_not_modified":True,
        },receipt)
        version=run["version"]+1
        cur.execute(
            """UPDATE procedure_runs SET status=%s,version=%s,
                 updated_at=clock_timestamp() WHERE run_id=%s""",
            (new,version,command.run_id),
        )
        action={
            "PAUSE":"RUN_PAUSED","RESUME":"RUN_RESUMED",
            "CANCEL":"RUN_CANCELLED","FINISH":"RUN_USER_CONFIRMED",
        }[command.action]
        return ProcedureRunResult(run_id=command.run_id,version=version,
                                  event_id=event,action=action)
    return _command(
        connection,actor,command,"procedure.run.status.v1",apply,
        result_type=ProcedureRunResult,
    )


def read_procedure_run(connection: Connection, actor: TrustedActor,
                       instance_id: UUID, run_id: UUID) -> ProcedureRunView:
    """Recover authoritative run/step progress after a restart or session gap."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            run=_run(cur,actor,instance_id,run_id,lock=False)
            cur.execute(
                """SELECT * FROM procedure_run_steps WHERE run_id=%s
                   ORDER BY position""",(run_id,),
            )
            rows=cur.fetchall()
            steps=tuple(ProcedureRunStepView(
                step_id=row["step_id"],position=row["position"],
                step=ProcedureStep(
                    step_id=row["step_id"],instruction=row["instruction"],
                    executor=row["executor"],success_test=row["success_test"],
                    capability=row["capability"],
                ),status=row["status"],version=row["version"],
                last_step_event_id=row["last_step_event_id"],
            ) for row in rows)
            cur.execute(
                """SELECT conversation_id,thread_id FROM procedure_run_thread_links
                   WHERE run_id=%s ORDER BY linked_at,conversation_id,thread_id""",
                (run_id,),
            )
            links=tuple(ProcedureRunThreadLink(**x) for x in cur.fetchall())
            next_step=next(
                (x.step_id for x in steps if x.status!="USER_CONFIRMED"),None,
            )
            return ProcedureRunView(
                run_id=run_id,template_id=run["template_id"],
                template_version=run["template_version"],
                variant_id=run["variant_id"],variant_revision=run["variant_revision"],
                task_id=run["task_id"],status=run["status"],version=run["version"],
                steps=steps,thread_links=links,next_step_id=next_step,
            )


def list_procedure_runs(connection: Connection, actor: TrustedActor,
                        instance_id: UUID, *, limit: int=50
                        ) -> tuple[ProcedureRunView, ...]:
    """Bounded selected-persona run recovery, including dormant conversations.

    Keep scope explicit; use one transaction for a consistent set of handles.
    """
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=50:
        raise ValueError("procedure run list limit must be 1..50")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT run_id FROM procedure_runs
                   WHERE user_id=%s AND instance_id=%s
                   ORDER BY updated_at DESC,run_id LIMIT %s""",
                (actor.user_id,instance_id,limit),
            )
            ids=[row["run_id"] for row in cur.fetchall()]
    # Each read has its own short transaction so nothing is held during
    # inference; callers select the relevant run from the bounded handles.
    return tuple(read_procedure_run(connection,actor,instance_id,run_id)
                 for run_id in ids)
