"""MVP-06B: ProcedureRun-owned, delivered guidance and bounded user reports.

A response binding records which pinned step/status was shown, not whether
the action succeeded. The existing owner contracts remain the only writers
of run/step progress. Conversation owns delivery and thread attention.
"""
from datetime import timedelta
from uuid import UUID

from .procedure_guidance_contracts import parse_guidance,CONFIRMATION,STEP_REPORTS
from .procedure_run_contracts import (
    RecordProcedureStepCommand,AcknowledgeProcedureInstructionCommand,
    SetProcedureRunStatusCommand,ProcedureRunResult,
)
from .runtime import PermissionDenied

_FINISHED={"USER_CONFIRMED","VERIFIED"}
_TERMINAL={"USER_CONFIRMED","CANCELLED"}
_UNBOUND="I can't identify a current guided procedure. Use 'Continue procedure: <title or run ID>'."
_STALE="The tracked progress changed. Continue the named procedure to see its current step."


def _previous(cur,*,user_id,instance_id,conversation_id,turn_id,generation):
    """Exactly adjacent, acknowledged response; no guess from model text."""
    cur.execute(
        """SELECT g.*,p.input_text AS prior_input,p.created_at AS prior_at,
                  r.response_text,t.input_text AS current_input,t.created_at AS current_at
           FROM conversation_turns t
           JOIN conversation_turns p ON p.conversation_id=t.conversation_id
             AND p.user_id=t.user_id AND p.instance_id=t.instance_id
             AND p.sequence=t.sequence-1
           JOIN procedure_guidance_responses g ON g.turn_id=p.turn_id
             AND g.conversation_id=p.conversation_id AND g.user_id=p.user_id AND g.instance_id=p.instance_id
           JOIN conversation_responses r ON r.response_id=g.response_id
             AND r.turn_id=g.turn_id AND r.conversation_id=g.conversation_id
             AND r.user_id=g.user_id AND r.instance_id=g.instance_id
           JOIN conversations c ON c.conversation_id=t.conversation_id
             AND c.user_id=t.user_id AND c.instance_id=t.instance_id
             AND c.last_input_turn_id=t.turn_id
           WHERE t.turn_id=%s AND t.user_id=%s AND t.instance_id=%s
             AND t.conversation_id=%s AND t.selection_generation=%s AND t.status='PENDING'
             AND r.selection_generation=t.selection_generation
             AND r.delivery_status='DELIVERED' AND g.bound_at IS NOT NULL
             AND r.delivered_at<=t.created_at AND g.bound_at<=t.created_at
             AND NOT EXISTS (
                 SELECT 1 FROM conversation_assistant_followups f
                 WHERE f.user_id=t.user_id AND f.instance_id=t.instance_id
                   AND f.conversation_id=t.conversation_id
                   AND f.delivered_at>r.delivered_at AND f.delivered_at<=t.created_at
             )""",
        (turn_id,user_id,instance_id,conversation_id,generation),
    )
    return cur.fetchone()


def _focused_run(cur,*,user_id,instance_id,conversation_id):
    """Reuse the existing explicit Conversation/ProcedureRun link."""
    cur.execute(
        """SELECT r.run_id FROM conversations c
           JOIN procedure_run_thread_links l ON l.conversation_id=c.conversation_id
             AND l.thread_id=c.focus_thread_id AND l.user_id=c.user_id AND l.instance_id=c.instance_id
           JOIN procedure_runs r ON r.run_id=l.run_id AND r.user_id=l.user_id
             AND r.instance_id=l.instance_id
           WHERE c.conversation_id=%s AND c.user_id=%s AND c.instance_id=%s
             AND r.status IN ('ACTIVE','PAUSED','NEEDS_REVIEW')
           ORDER BY r.run_id LIMIT 2""",
        (conversation_id,user_id,instance_id),
    )
    rows=cur.fetchall()
    return rows[0]["run_id"] if len(rows)==1 else None


def should_route_guidance(cur,*,user_id,instance_id,conversation_id,turn_id,generation,text):
    action=parse_guidance(text)
    if action is None:
        return False
    if action.title is not None or action.kind.startswith("CONFIRM_"):
        return True
    if action.kind in STEP_REPORTS or action.kind in ("PAUSE","RESUME","CANCEL"):
        # Bare pause/resume/cancel may refer to other conversational work;
        # require a delivered binding. Explicit procedure-specific claims
        # can receive a canonical abstention if unbound.
        if text.strip().casefold().rstrip(".!") in ("pause","resume","cancel"):
            return _previous(cur,user_id=user_id,instance_id=instance_id,
                conversation_id=conversation_id,turn_id=turn_id,generation=generation) is not None
        return True
    # Bare navigation can also belong to ordinary, untracked discussion.
    return _previous(cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,turn_id=turn_id,generation=generation) is not None or (
        _focused_run(cur,user_id=user_id,instance_id=instance_id,conversation_id=conversation_id)
        is not None
    )


def _named_run(cur,actor,instance_id,title):
    try:
        run_id=UUID(title)
    except ValueError:
        run_id=None
    cur.execute(
        """SELECT r.run_id FROM procedure_runs r
           JOIN procedure_template_versions v ON v.template_id=r.template_id
             AND v.version=r.template_version
           WHERE r.user_id=%s AND r.instance_id=%s
             AND r.status IN ('ACTIVE','PAUSED','NEEDS_REVIEW')
             AND ((%s::uuid IS NOT NULL AND r.run_id=%s)
                  OR (%s::uuid IS NULL AND lower(v.definition->>'title')=lower(%s)))
           ORDER BY r.run_id LIMIT 2""",
        (actor.user_id,instance_id,run_id,run_id,run_id,title),
    )
    rows=cur.fetchall()
    return rows[0]["run_id"] if len(rows)==1 else None


def _title(cur,run):
    cur.execute(
        """SELECT definition->>'title' AS title FROM procedure_template_versions
           WHERE template_id=%s AND version=%s""",
        (run["template_id"],run["template_version"]),
    )
    return cur.fetchone()["title"]


def _steps(cur,run):
    cur.execute("SELECT * FROM procedure_run_steps WHERE run_id=%s ORDER BY position",
                (run["run_id"],))
    return list(cur.fetchall())


def _outstanding(steps):
    return next((s for s in steps if s["status"] not in _FINISHED),None)


def _report_text(step):
    return (f"Step {step['position']} is recorded as user-reported done, not independently verified. "
            f"Check: {step['success_test']}. "
            "If it passed, say 'I confirm this step passed its check.' "
            "For an older report or a new conversation, first say 'I completed this step.'")


def _status_challenge(title,action,run_id):
    verb={"PAUSE":"Pause","RESUME":"Resume","CANCEL":"Cancel"}[action]
    return (f"{verb} the tracked procedure '{title}' (run {run_id})? "
            f"Reply '{CONFIRMATION[action]}' to confirm. Step progress is unchanged.")


def _guide_text(step,*,review=False):
    heading="Review step" if review else "Step"
    return (f"{heading} {step['position']} ({step['status']}): {step['instruction']}\n"
            f"Check: {step['success_test']}."
            + ("" if review else "\nWhen done, say 'I completed this step.'"))


def _bind(cur,actor,context,response_id,run,kind,step=None):
    cur.execute(
        """INSERT INTO procedure_guidance_responses(
               response_id,user_id,instance_id,conversation_id,turn_id,run_id,
               run_version,step_id,step_version,kind
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (response_id,actor.user_id,context.instance_id,context.conversation_id,
         context.turn_id,run["run_id"],run["version"],
         step["step_id"] if step else None,step["version"] if step else None,kind),
    )


def _base_command(context,scoped,run,*,source_turn=None,source_quote=None,approval=None):
    return dict(instance_id=context.instance_id,expected_generation=context.selection_generation,
        idempotency_key=f"guidance:{context.turn_id}",conversation_id=context.conversation_id,
        evidence_turn_id=source_turn or context.turn_id,
        evidence_quote=source_quote or scoped["input_text"],approval_turn_id=approval,
        run_id=run["run_id"],expected_run_version=run["version"])


def _last_report(cur,step):
    cur.execute(
        """SELECT source_turn_id,source_conversation_id,source_quote,kind,step_version
           FROM procedure_run_step_events WHERE step_event_id=%s AND run_id=%s
             AND step_id=%s AND user_id=%s AND instance_id=%s""",
        (step["last_step_event_id"],step["run_id"],step["step_id"],
         step["user_id"],step["instance_id"]),
    )
    return cur.fetchone()


def _validate_guidance_approval(cur,actor,cmd,source):
    """Owner-bound alternative to the public digest approval.

    Reconstruct the action from the exact current delivered challenge, not
    an arbitrary model label or a caller-provided approval boolean.
    """
    from .procedure_run_state import _run,_step
    if cmd.approval_turn_id is None or cmd.approval_turn_id==cmd.evidence_turn_id:
        raise PermissionDenied("guided change requires a distinct approval turn")
    prior=_previous(cur,user_id=actor.user_id,instance_id=cmd.instance_id,
        conversation_id=cmd.conversation_id,turn_id=cmd.approval_turn_id,
        generation=cmd.expected_generation)
    action=parse_guidance(prior["current_input"]) if prior else None
    if (prior is None or action is None or prior["run_id"]!=cmd.run_id
            or prior["run_version"]!=cmd.expected_run_version
            or prior["current_at"]<=source["created_at"]
            or prior["current_at"]>source["created_at"]+timedelta(days=1)):
        raise PermissionDenied("guided approval does not match delivered challenge")
    run=_run(cur,actor,cmd.instance_id,cmd.run_id)
    if run["version"]!=cmd.expected_run_version:
        raise PermissionDenied("guided approval is stale")
    if isinstance(cmd,SetProcedureRunStatusCommand):
        if (cmd.action not in CONFIRMATION or action.kind!="CONFIRM_"+cmd.action
                or prior["kind"]!=cmd.action or cmd.evidence_turn_id!=prior["turn_id"]
                or cmd.evidence_quote!=prior["prior_input"]
                or prior["response_text"]!=_status_challenge(_title(cur,run),cmd.action,run["run_id"])):
            raise PermissionDenied("guided status confirmation mismatch")
    elif isinstance(cmd,RecordProcedureStepCommand):
        step=_step(cur,cmd.run_id,cmd.step_id)
        report=_last_report(cur,step)
        if (cmd.outcome!="USER_CONFIRMED" or cmd.detail is not None
                or action.kind!="USER_CONFIRMED" or prior["kind"]!="REPORT"
                or prior["step_id"]!=cmd.step_id or prior["step_version"]!=cmd.expected_step_version
                or step["version"]!=cmd.expected_step_version or step["executor"]!="USER"
                or step["status"]!="USER_REPORTED_DONE" or report is None
                or report["kind"]!="USER_REPORTED_DONE" or report["step_version"]!=step["version"]
                or report["source_conversation_id"]!=cmd.conversation_id
                or report["source_turn_id"]!=cmd.evidence_turn_id
                or report["source_quote"]!=cmd.evidence_quote
                or prior["response_text"]!=_report_text(step)):
            raise PermissionDenied("guided step confirmation mismatch")
    else:
        raise PermissionDenied("unsupported conversational procedure approval")


def _renew_report_apply(cur,actor,cmd,receipt):
    """Refresh explicit completion evidence, not the outcome or definition.

    A report from another conversation or outside the approval window
    cannot be used as if it were authenticated in the current conversation.
    Re-reporting a completed user action supplies a new provenance event.
    """
    from .procedure_run_state import _step_admission,_evidence_step_event
    run,step=_step_admission(cur,actor,cmd)
    prior=_previous(cur,user_id=actor.user_id,instance_id=cmd.instance_id,
        conversation_id=cmd.conversation_id,turn_id=cmd.evidence_turn_id,
        generation=cmd.expected_generation)
    action=parse_guidance(prior["current_input"]) if prior else None
    if (prior is None or prior["kind"]!="REPORT" or prior["run_id"]!=cmd.run_id
            or prior["run_version"]!=run["version"] or prior["step_id"]!=cmd.step_id
            or prior["step_version"]!=step["version"] or action is None
            or action.kind!="USER_REPORTED_DONE" or cmd.outcome!="USER_REPORTED_DONE"
            or step["executor"]!="USER" or step["status"]!="USER_REPORTED_DONE"
            or prior["response_text"]!=_report_text(step)):
        raise PermissionDenied("renewed step report requires a delivered current report")
    return _evidence_step_event(cur,actor,cmd,run,step,receipt,
        kind="USER_REPORTED_DONE",after="USER_REPORTED_DONE")


def _owner_command(cur,actor,cmd,kind,apply,*,approval=False):
    from .procedure_state import _command_in_transaction
    return _command_in_transaction(cur,actor,cmd,"procedure.guidance."+kind+".v1",
        lambda cursor,receipt: apply(cursor,actor,cmd,receipt),
        result_type=ProcedureRunResult,requires_approval=approval,
        approval_validator=_validate_guidance_approval if approval else None)


def apply_guidance(cur,actor,context,scoped,response_id,action):
    """Conversation already fences the selected generation/latest turn.

    Lock the run before reading steps, and never hold this lock over a model
    call. A stale report/confirmation is refused; navigation can refresh.
    """
    from .procedure_run_state import _run,_record_step_apply,_status_apply
    prior=_previous(cur,user_id=actor.user_id,instance_id=context.instance_id,
        conversation_id=context.conversation_id,turn_id=context.turn_id,
        generation=context.selection_generation)
    run_id=(_named_run(cur,actor,context.instance_id,action.title) if action.title
            else prior["run_id"] if prior else None)
    if run_id is None and action.title is None and action.kind in (
            "NEXT","REPEAT","BACK","SKIP","CLARIFY"):
        run_id=_focused_run(cur,user_id=actor.user_id,instance_id=context.instance_id,
                            conversation_id=context.conversation_id)
    if run_id is None:
        return _UNBOUND
    run=_run(cur,actor,context.instance_id,run_id)
    title=_title(cur,run)
    steps=_steps(cur,run)
    outstanding=_outstanding(steps)
    current=next((s for s in steps if prior and prior["run_id"]==run_id
                  and s["step_id"]==prior["step_id"]),None)
    kind=action.kind

    if run["status"] in _TERMINAL:
        return f"The tracked procedure '{title}' is {run['status']}. No step was changed."
    if kind.startswith("CONFIRM_"):
        requested=kind.removeprefix("CONFIRM_")
        eligible={"PAUSE":("ACTIVE","NEEDS_REVIEW"),"RESUME":("PAUSED",),
                  "CANCEL":("ACTIVE","PAUSED","NEEDS_REVIEW")}
        if (prior is None or prior["kind"]!=requested or prior["run_version"]!=run["version"]
                or run["status"] not in eligible.get(requested,())
                or scoped["turn_at"]>prior["prior_at"]+timedelta(days=1)):
            return _STALE
        cmd=SetProcedureRunStatusCommand(**_base_command(context,scoped,run,
            source_turn=prior["turn_id"],source_quote=prior["prior_input"],
            approval=context.turn_id),action=requested)
        result=_owner_command(cur,actor,cmd,"status",_status_apply,approval=True)
        run=_run(cur,actor,context.instance_id,run_id)
        steps=_steps(cur,run)
        outstanding=_outstanding(steps)
        if requested!="RESUME":
            _bind(cur,actor,context,response_id,run,"STATUS")
            return f"Procedure '{title}' is now {run['status'].lower()}. Step progress is unchanged."
        kind="NEXT"
    if kind in CONFIRMATION:
        eligible={"PAUSE":("ACTIVE","NEEDS_REVIEW"),"RESUME":("PAUSED",),
                  "CANCEL":("ACTIVE","PAUSED","NEEDS_REVIEW")}
        if kind=="RESUME" and run["status"] in ("ACTIVE","NEEDS_REVIEW"):
            kind="NEXT"
        elif run["status"] in eligible[kind]:
            _bind(cur,actor,context,response_id,run,kind)
            return _status_challenge(title,kind,run["run_id"])
        else:
            _bind(cur,actor,context,response_id,run,"STATUS")
            return f"Procedure '{title}' is {run['status'].lower()}. No status change is needed."
    if run["status"]=="PAUSED":
        _bind(cur,actor,context,response_id,run,"STATUS")
        return f"Procedure '{title}' is paused. Say 'Resume this procedure.' to request resumption."
    if outstanding is None:
        _bind(cur,actor,context,response_id,run,"STATUS")
        return "All recorded steps are user-confirmed or verified. The run is not finalized automatically."
    if kind in STEP_REPORTS:
        if (prior is None or prior["run_version"]!=run["version"] or current is None
                or prior["step_version"]!=current["version"]
                or current["step_id"]!=outstanding["step_id"] or current["executor"]!="USER"):
            return _STALE
        if kind=="USER_CONFIRMED":
            report=_last_report(cur,current)
            if (prior["kind"]!="REPORT" or current["status"]!="USER_REPORTED_DONE"
                    or report is None or report["kind"]!="USER_REPORTED_DONE"
                    or report["source_conversation_id"]!=context.conversation_id
                    or report["step_version"]!=current["version"]):
                if current["status"]=="USER_REPORTED_DONE":
                    _bind(cur,actor,context,response_id,run,"REPORT",current)
                    return _report_text(current)
                return "I need a current user completion report and delivered check before confirmation."
            # The existing owner requires report and approval in the same
            # conversation. A fresh explicit re-report can renew the source.
            cur.execute("SELECT created_at FROM conversation_turns WHERE turn_id=%s",
                        (report["source_turn_id"],))
            if scoped["turn_at"]>cur.fetchone()["created_at"]+timedelta(days=1):
                _bind(cur,actor,context,response_id,run,"REPORT",current)
                return _report_text(current)
            cmd=RecordProcedureStepCommand(**_base_command(context,scoped,run,
                source_turn=report["source_turn_id"],source_quote=report["source_quote"],
                approval=context.turn_id),step_id=current["step_id"],
                expected_step_version=current["version"],outcome=kind)
            _owner_command(cur,actor,cmd,"confirm",_record_step_apply,approval=True)
            run=_run(cur,actor,context.instance_id,run_id)
            steps=_steps(cur,run)
            outstanding=_outstanding(steps)
            if outstanding is None:
                _bind(cur,actor,context,response_id,run,"STATUS")
                return "Step user-confirmed. All recorded steps are now confirmed or verified; the run is not finalized automatically."
            kind="NEXT"
        else:
            if prior["kind"]=="REPORT" and current["status"]=="USER_REPORTED_DONE":
                if kind!="USER_REPORTED_DONE":
                    return "The prior completion report requires an explicit evidence correction before a different outcome."
                report=_last_report(cur,current)
                if report is None or report["kind"]!="USER_REPORTED_DONE":
                    return "The saved completion evidence needs review before it can be renewed."
                cur.execute("SELECT created_at FROM conversation_turns WHERE turn_id=%s",
                            (report["source_turn_id"],))
                report_at=cur.fetchone()["created_at"]
                if (report["source_conversation_id"]!=context.conversation_id
                        or scoped["turn_at"]>report_at+timedelta(days=1)):
                    cmd=RecordProcedureStepCommand(**_base_command(context,scoped,run),
                        step_id=current["step_id"],expected_step_version=current["version"],
                        outcome=kind)
                    _owner_command(cur,actor,cmd,"renew-report",_renew_report_apply)
                    run=_run(cur,actor,context.instance_id,run_id)
                    current=_outstanding(_steps(cur,run))
                _bind(cur,actor,context,response_id,run,"REPORT",current)
                return _report_text(current)
            if prior["kind"]!="GUIDE" or current["status"]=="NOT_STARTED":
                return "First request the current unresolved step; a review is not a completion report."
            if current["status"]=="USER_REPORTED_DONE":
                return "The prior completion report requires an explicit evidence correction before a different outcome."
            if current["status"]!=kind:
                cmd=RecordProcedureStepCommand(**_base_command(context,scoped,run),
                    step_id=current["step_id"],expected_step_version=current["version"],
                    outcome=kind,detail=action.detail)
                _owner_command(cur,actor,cmd,"report",_record_step_apply)
                run=_run(cur,actor,context.instance_id,run_id)
                outstanding=_outstanding(_steps(cur,run))
            if kind=="USER_REPORTED_DONE":
                _bind(cur,actor,context,response_id,run,"REPORT",outstanding)
                return _report_text(outstanding)
            _bind(cur,actor,context,response_id,run,"GUIDE",outstanding)
            if kind in ("FAILED","BLOCKED"):
                return ("Obstacle recorded; the step remains unresolved.\n"
                        + _guide_text(outstanding))
            return "Attempt recorded; the step remains unresolved.\n"+_guide_text(outstanding)

    if kind=="BACK" or (kind in ("REPEAT","CLARIFY") and prior
                         and prior["run_id"]==run_id and prior["kind"]=="REVIEW"):
        anchor=current or outstanding
        target=anchor
        if kind=="BACK":
            target=next((s for s in reversed(steps) if s["position"]<anchor["position"]),anchor)
        _bind(cur,actor,context,response_id,run,"REVIEW",target)
        return _guide_text(target,review=True)+"\nReview only; recorded progress is unchanged."
    if outstanding["executor"]=="AGENT":
        _bind(cur,actor,context,response_id,run,"STATUS",outstanding)
        return "The next unresolved step requires authorized agent execution. No operation was started."
    if outstanding["status"]=="USER_REPORTED_DONE":
        _bind(cur,actor,context,response_id,run,"REPORT",outstanding)
        return _report_text(outstanding)
    _bind(cur,actor,context,response_id,run,"GUIDE",outstanding)
    prefix="Required steps cannot be marked complete by skipping.\n" if kind=="SKIP" else ""
    if kind=="CLARIFY":
        prefix="Here is the pinned instruction and its success check.\n"
    return prefix+_guide_text(outstanding)


def bind_delivered_guidance(cur,actor,row):
    """Acknowledge instruction only after actual delivery of exact step.

    Late delivery may still be recorded by Conversation, but cannot create
    a current binding or overwrite newer run progress.
    """
    from .procedure_run_state import _run,_step,_instruction_apply
    cur.execute(
        """SELECT g.*,t.input_text,c.last_input_turn_id,c.revision
           FROM procedure_guidance_responses g
           JOIN conversation_turns t ON t.turn_id=g.turn_id
             AND t.conversation_id=g.conversation_id AND t.user_id=g.user_id AND t.instance_id=g.instance_id
           JOIN conversations c ON c.conversation_id=g.conversation_id
             AND c.user_id=g.user_id AND c.instance_id=g.instance_id
           WHERE g.response_id=%s AND g.user_id=%s AND g.instance_id=%s""",
        (row["response_id"],actor.user_id,row["instance_id"]),
    )
    binding=cur.fetchone()
    if (binding is None or binding["bound_at"] is not None
            or binding["last_input_turn_id"]!=row["turn_id"]
            or binding["revision"]!=row["context_revision"]+2):
        return
    run=_run(cur,actor,row["instance_id"],binding["run_id"])
    if run["version"]!=binding["run_version"]:
        return
    step=None
    if binding["step_id"] is not None:
        step=_step(cur,run["run_id"],binding["step_id"])
        if step["version"]!=binding["step_version"]:
            return
    if (binding["kind"]=="GUIDE" and step is not None
            and step["status"]=="NOT_STARTED" and step["executor"]=="USER"
            and run["status"] in ("ACTIVE","NEEDS_REVIEW")):
        if step["instruction"] not in row["response_text"]:
            return
        command=AcknowledgeProcedureInstructionCommand(
            instance_id=row["instance_id"],expected_generation=row["selection_generation"],
            idempotency_key=f"guidance-delivery:{row['response_id']}",
            conversation_id=row["conversation_id"],evidence_turn_id=row["turn_id"],
            evidence_quote=binding["input_text"],run_id=run["run_id"],
            expected_run_version=run["version"],step_id=step["step_id"],
            expected_step_version=step["version"],response_id=row["response_id"],
        )
        result=_owner_command(cur,actor,command,"delivery",_instruction_apply)
        binding["run_version"]=result.version
        binding["step_version"]=result.step_version
    cur.execute(
        """UPDATE procedure_guidance_responses SET bound_at=clock_timestamp(),
                  run_version=%s,step_version=%s
           WHERE response_id=%s AND user_id=%s AND instance_id=%s AND bound_at IS NULL""",
        (binding["run_version"],binding["step_version"],row["response_id"],
         actor.user_id,row["instance_id"]),
    )
