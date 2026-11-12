"""P3-04 real PostgreSQL acceptance for durable method progress and dialogue."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.conversation import (
    acknowledge_text_delivery, respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply
from persona_agent.dialogue_contracts import OpenThreadCommand
from persona_agent.dialogue_state import open_thread
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.procedure_contracts import (
    ParameterSpec, ProcedureDefinition, ProcedureStep,
)
from persona_agent.procedure_state import (
    create_personalized_approach, publish_shared_procedure,
    revise_personalized_approach,
)
from persona_agent.procedure_run_contracts import (
    AcknowledgeProcedureInstructionCommand, CorrectProcedureStepCommand,
    LinkProcedureThreadCommand, RecordProcedureStepCommand,
    SetProcedureRunStatusCommand, StartProcedureRunCommand,
)
from persona_agent.procedure_run_state import (
    acknowledge_procedure_instruction, correct_procedure_step,
    link_procedure_thread, list_procedure_runs, read_procedure_run,
    record_procedure_step, set_procedure_run_status, start_procedure_run,
)
from persona_agent.runtime import PermissionDenied, StaleSelection, VersionConflict
from persona_agent.task_contracts import SetTaskPriorityCommand
from persona_agent.task_state import list_tasks, set_task_priority

from test_phase3_procedures import (
    actor, approve, call, definition, publication, revise_command,
    seed, turn, variant_command,
)
from test_phase3_tasks import (
    approve as approve_task, make_goal, make_task, report_done,
)


def published(dsn,policy="STABLE",*,defined=None):
    user,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conv,template,0,defined or definition(policy),
    ))
    return user,persona,instance,conv,template


def start_command(dsn,user,instance,conv,template,*,version=1,
                  variant=None,revision=None,task=None,parameters=None):
    src=turn(dsn,user,instance,conv,"Please guide me through this procedure.")
    command=StartProcedureRunCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=src.turn_id,
        evidence_quote="Please guide me through this procedure",
        run_id=uuid4(),template_id=template,template_version=version,
        variant_id=variant,variant_revision=revision,
        task_id=task,parameters=parameters or {},
    )
    return approve(dsn,user,instance,command)


def record(dsn,user,instance,conv,run,version,step,step_version,outcome,detail=None):
    src=turn(dsn,user,instance,conv,"I worked on this procedure step.")
    command=RecordProcedureStepCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=src.turn_id,
        evidence_quote="I worked on this procedure step",run_id=run,
        expected_run_version=version,step_id=step,
        expected_step_version=step_version,outcome=outcome,detail=detail,
    )
    if outcome=="USER_CONFIRMED":
        command=approve(dsn,user,instance,command)
    return call(dsn,record_procedure_step,actor(user),command)


def correction(dsn,user,instance,conv,run,version,step,step_version,event_id):
    src=turn(dsn,user,instance,conv,"My previous step report was mistaken.")
    cmd=CorrectProcedureStepCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=src.turn_id,
        evidence_quote="My previous step report was mistaken",run_id=run,
        expected_run_version=version,step_id=step,
        expected_step_version=step_version,
        superseded_step_event_id=event_id,
        reason="I tested the wrong hostname and need to repeat the step",
    )
    return approve(dsn,user,instance,cmd)


def run_status(dsn,user,instance,conv,run,version,action):
    src=turn(dsn,user,instance,conv,"Change the status of this guided run.")
    cmd=SetProcedureRunStatusCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=src.turn_id,
        evidence_quote="Change the status of this guided run",
        run_id=run,expected_run_version=version,action=action,
    )
    return call(dsn,set_procedure_run_status,actor(user),
                approve(dsn,user,instance,cmd))


def test_version_pinning_and_restart_survives_shared_and_variant_updates(dsn):
    user,persona,instance,conv,template=published(dsn,"PARAMETERIZED")
    personal=variant_command(dsn,user,instance,conv,template,1,
                             params={"hostname":"localhost","days":30})
    call(dsn,create_personalized_approach,actor(user),personal)
    start=start_command(dsn,user,instance,conv,template,
                        variant=personal.variant_id,revision=1)
    created=call(dsn,start_procedure_run,actor(user),start)
    assert created.action=="RUN_STARTED" and created.version==1
    assert call(dsn,start_procedure_run,actor(user),start).replayed
    before=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert before.steps[0].step.instruction.endswith("30 days")
    assert before.steps[0].status=="NOT_STARTED"
    assert before.next_step_id=="generate"
    assert before.operation_dispatch_authorized is False
    assert before.task_completion_changed is False

    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conv,template,1,
        definition("PARAMETERIZED",suffix=" changed"),
    ))
    changed=revise_command(dsn,user,instance,conv,personal.variant_id,1,2,
                           params={"hostname":"localhost","days":60})
    call(dsn,revise_personalized_approach,actor(user),changed)
    after=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert after.template_version==1 and after.variant_revision==1
    assert after.steps[0].step.instruction==before.steps[0].step.instruction
    assert any(x.run_id==start.run_id for x in
               call(dsn,list_procedure_runs,actor(user),instance))
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "UPDATE procedure_run_steps SET instruction='tampered' "
                "WHERE run_id=%s AND step_id='generate'",(start.run_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "UPDATE procedure_runs SET template_version=2 WHERE run_id=%s",
                (start.run_id,),
            )


def test_user_steps_are_not_task_completion_and_correction_preserves_history(dsn):
    user,persona,instance,conv,template=published(dsn)
    goal=make_goal(dsn,user,instance,conv)
    task,_,_=make_task(dsn,user,instance,conv,goal,1)
    start=start_command(dsn,user,instance,conv,template,task=task)
    run=start.run_id
    call(dsn,start_procedure_run,actor(user),start)
    first=record(dsn,user,instance,conv,run,1,"generate",1,"USER_REPORTED_DONE")
    assert first.version==2
    assert first.step_event_id
    second=record(dsn,user,instance,conv,run,2,"generate",2,"USER_CONFIRMED")
    assert second.version==3
    third=record(dsn,user,instance,conv,run,3,"check",1,"USER_REPORTED_DONE")
    fourth=record(dsn,user,instance,conv,run,4,"check",2,"USER_CONFIRMED")
    complete=run_status(dsn,user,instance,conv,run,5,"FINISH")
    assert complete.action=="RUN_USER_CONFIRMED"
    assert call(dsn,read_procedure_run,actor(user),instance,run).status=="USER_CONFIRMED"
    task_before=call(dsn,list_tasks,actor(user),instance,goal)[0]
    assert task_before.status=="PLANNED" and not task_before.effective_completion

    corrected=call(dsn,correct_procedure_step,actor(user),
                   correction(dsn,user,instance,conv,run,6,"generate",3,
                              second.step_event_id))
    assert corrected.action=="STEP_CORRECTED" and corrected.version==7
    view=call(dsn,read_procedure_run,actor(user),instance,run)
    assert view.status=="NEEDS_REVIEW"
    assert view.steps[0].status=="NEEDS_REVIEW"
    assert view.steps[1].status=="USER_CONFIRMED"
    assert not view.steps[0].independently_verified
    with pytest.raises(VersionConflict,match="no current claim"):
        call(dsn,correct_procedure_step,actor(user),
             correction(dsn,user,instance,conv,run,7,"generate",4,
                        first.step_event_id))
    rereport=record(dsn,user,instance,conv,run,7,"generate",4,"USER_REPORTED_DONE")
    assert rereport.version==8
    reconfirm=record(dsn,user,instance,conv,run,8,"generate",5,"USER_CONFIRMED")
    assert reconfirm.version==9
    assert run_status(dsn,user,instance,conv,run,9,"FINISH").version==10
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status=="PLANNED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM procedure_run_step_events "
            "WHERE run_id=%s AND step_id='generate'",(run,),
        ).fetchone()[0]==5
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "DELETE FROM procedure_run_step_events WHERE run_id=%s",(run,),
            )


def test_delivery_ack_requires_delivered_exact_instruction_not_generated_text(dsn):
    user,persona,instance,conv,template=published(dsn)
    start=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),start)
    src=turn(dsn,user,instance,conv,"Can you show me the certificate command?")
    class FixedModel:
        def generate(self,messages):
            return ModelReply(model_id="run-test",text="Generate the certificate")
    response=call(dsn,respond_to_text,actor(user),src.turn_id,FixedModel())
    cmd=AcknowledgeProcedureInstructionCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=src.turn_id,
        evidence_quote="Can you show me the certificate command",
        run_id=start.run_id,expected_run_version=1,
        step_id="generate",expected_step_version=1,response_id=response.response_id,
    )
    with pytest.raises(VersionConflict,match="not acknowledged"):
        call(dsn,acknowledge_procedure_instruction,actor(user),cmd)
    assert call(dsn,read_procedure_run,actor(user),instance,start.run_id
                ).steps[0].status=="NOT_STARTED"
    call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    done=call(dsn,acknowledge_procedure_instruction,actor(user),cmd)
    assert done.action=="INSTRUCTION_DELIVERED"
    assert call(dsn,acknowledge_procedure_instruction,actor(user),cmd).replayed
    view=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert view.steps[0].status=="INSTRUCTED"
    assert view.steps[0].independently_verified is False
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM task_outcome_reports",
        ).fetchone()[0]==0


def test_run_thread_links_across_conversations_and_selected_persona_scope(dsn):
    user,persona,instance,conv,template=published(dsn)
    start=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),start)
    for idx,cid in enumerate((conv,uuid4()),1):
        source=turn(dsn,user,instance,cid,"Talk about my Nginx setup progress.")
        with psycopg.connect(dsn,autocommit=True) as conn:
            rev=conn.execute(
                "SELECT revision FROM conversations WHERE conversation_id=%s",
                (cid,),
            ).fetchone()[0]
        thread=uuid4()
        call(dsn,open_thread,actor(user),OpenThreadCommand(
            instance_id=instance,conversation_id=cid,
            expected_generation=1,expected_revision=rev,
            evidence_turn_id=source.turn_id,idempotency_key=uuid4().hex,
            thread_id=thread,title="Nginx TLS setup",
        ))
        cmd=LinkProcedureThreadCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,conversation_id=cid,
            evidence_turn_id=source.turn_id,
            evidence_quote="Talk about my Nginx setup progress",
            run_id=start.run_id,expected_run_version=idx,thread_id=thread,
        )
        result=call(dsn,link_procedure_thread,actor(user),cmd)
        assert result.version==idx+1
    view=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert len(view.thread_links)==2
    assert view.next_step_id=="generate"
    assert view.version==3
    outsider,_,outside_instance=seed(dsn,persona_id=persona)
    with pytest.raises(PermissionDenied):
        call(dsn,read_procedure_run,actor(outsider),outside_instance,start.run_id)
    assert call(dsn,list_procedure_runs,actor(outsider),outside_instance)==()


def test_step_cas_concurrency_and_run_pause_recovery(dsn):
    user,persona,instance,conv,template=published(dsn)
    start=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),start)
    commands=[]
    for outcome in ("ATTEMPTED","USER_REPORTED_DONE"):
        source=turn(dsn,user,instance,conv,"I worked on the current step.")
        commands.append(RecordProcedureStepCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,conversation_id=conv,
            evidence_turn_id=source.turn_id,
            evidence_quote="I worked on the current step",
            run_id=start.run_id,expected_run_version=1,
            step_id="generate",expected_step_version=1,outcome=outcome,
        ))
    barrier=Barrier(2)
    def race(cmd):
        barrier.wait(timeout=20)
        try:
            return call(dsn,record_procedure_step,actor(user),cmd)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(race,commands[0])
        b=pool.submit(race,commands[1])
        outcomes=(a.result(timeout=20),b.result(timeout=20))
    assert sum(result is not None for result in outcomes)==1
    assert call(dsn,read_procedure_run,actor(user),instance,start.run_id).version==2
    paused=run_status(dsn,user,instance,conv,start.run_id,2,"PAUSE")
    assert paused.version==3
    with pytest.raises(VersionConflict,match="resume"):
        record(dsn,user,instance,conv,start.run_id,3,"check",1,"ATTEMPTED")
    assert run_status(dsn,user,instance,conv,start.run_id,3,"RESUME").version==4
    assert record(dsn,user,instance,conv,start.run_id,4,"check",1,
                  "ATTEMPTED").version==5


def test_agent_steps_remain_non_executable_and_no_false_user_verification(dsn):
    custom=ProcedureDefinition(
        title="Mixed method",purpose="Document user and agent steps",
        applicability="Development",limitations="No tool authority",
        policy="STABLE",preconditions=("Available input",),
        steps=(
            ProcedureStep(step_id="user_check",instruction="Check the input",
                          executor="USER",success_test="Input checked"),
            ProcedureStep(step_id="agent_action",instruction="Fetch documentation",
                          executor="AGENT",capability="browser.search",
                          success_test="Trusted tool result"),
        ),success_tests=("Documented result",),
    )
    user,persona,instance,conv,template=published(dsn,defined=custom)
    cmd=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),cmd)
    with pytest.raises(PermissionDenied,match="future operation-result"):
        record(dsn,user,instance,conv,cmd.run_id,1,"agent_action",1,
               "USER_REPORTED_DONE")
    with pytest.raises(VersionConflict,match="all user steps"):
        run_status(dsn,user,instance,conv,cmd.run_id,1,"FINISH")
    assert call(dsn,read_procedure_run,actor(user),instance,cmd.run_id
                ).operation_dispatch_authorized is False


def test_unrelated_step_activity_does_not_clear_outstanding_correction(dsn):
    user,persona,instance,conv,template=published(dsn)
    start=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),start)
    first=record(dsn,user,instance,conv,start.run_id,1,
                 "generate",1,"USER_REPORTED_DONE")
    confirmed=record(dsn,user,instance,conv,start.run_id,2,
                     "generate",2,"USER_CONFIRMED")
    fixed=call(dsn,correct_procedure_step,actor(user),correction(
        dsn,user,instance,conv,start.run_id,3,"generate",3,
        confirmed.step_event_id,
    ))
    assert fixed.version==4
    unrelated=record(dsn,user,instance,conv,start.run_id,4,
                     "check",1,"ATTEMPTED")
    assert unrelated.version==5
    view=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert view.status=="NEEDS_REVIEW"
    assert view.steps[0].status=="NEEDS_REVIEW"
    resumed=record(dsn,user,instance,conv,start.run_id,5,
                   "generate",4,"USER_REPORTED_DONE")
    assert resumed.version==6
    assert call(dsn,read_procedure_run,actor(user),instance,
                start.run_id).status=="ACTIVE"


def test_step_corrections_during_pause_do_not_implicitly_resume(dsn):
    user,persona,instance,conv,template=published(dsn)
    start=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),start)
    evidence=record(dsn,user,instance,conv,start.run_id,1,
                    "generate",1,"USER_REPORTED_DONE")
    paused=run_status(dsn,user,instance,conv,start.run_id,2,"PAUSE")
    assert paused.version==3
    corrected=call(dsn,correct_procedure_step,actor(user),correction(
        dsn,user,instance,conv,start.run_id,3,"generate",2,
        evidence.step_event_id,
    ))
    assert corrected.version==4
    view=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert view.status=="PAUSED"
    assert view.steps[0].status=="NEEDS_REVIEW"
    resumed=run_status(dsn,user,instance,conv,start.run_id,4,"RESUME")
    assert resumed.version==5
    assert call(dsn,read_procedure_run,actor(user),instance,
                start.run_id).status=="NEEDS_REVIEW"
    with pytest.raises(VersionConflict,match="invalid procedure run status"):
        run_status(dsn,user,instance,conv,start.run_id,5,"FINISH")
    recovered=record(dsn,user,instance,conv,start.run_id,5,
                     "generate",3,"USER_REPORTED_DONE")
    assert recovered.version==6
    assert call(dsn,read_procedure_run,actor(user),instance,
                start.run_id).status=="ACTIVE"


def test_rendered_step_limits_are_readable_at_maximum_snapshot_lengths(dsn):
    """Template constraints apply before interpolation; run limits after it."""
    long_definition=ProcedureDefinition(
        title="Long rendered method",
        purpose="Exercise parameterized step recovery at permitted boundaries",
        applicability="Local test setup",
        limitations="Test-only example",
        policy="PARAMETERIZED",
        preconditions=("Test parameter available",),
        parameters=(ParameterSpec(
            name="hostname",kind="TEXT",max_chars=256,
        ),),
        steps=(ProcedureStep(
            step_id="rendered",
            instruction="I"*690+"{hostname}{hostname}",
            executor="USER",
            success_test="S"*290+"{hostname}{hostname}",
        ),),
        success_tests=("Result can be inspected",),
    )
    user,persona,instance,conv,template=published(
        dsn,defined=long_definition,
    )
    command=start_command(
        dsn,user,instance,conv,template,
        parameters={"hostname":"x"*255},
    )
    created=call(dsn,start_procedure_run,actor(user),command)
    assert created.version==1
    # 690 + 2*255 == 1200; 290 + 2*255 == 800.
    for recovered in (
        call(dsn,read_procedure_run,actor(user),instance,command.run_id),
        next(r for r in call(dsn,list_procedure_runs,actor(user),instance)
             if r.run_id==command.run_id),
    ):
        step=recovered.steps[0].step
        assert len(step.instruction)==1200
        assert len(step.success_test)==800
        assert step.instruction.endswith("x"*255)
        assert step.success_test.endswith("x"*255)
        assert recovered.next_step_id=="rendered"
    # Progress must also remain readable after projection state updates.
    report=record(
        dsn,user,instance,conv,command.run_id,1,
        "rendered",1,"USER_REPORTED_DONE",
    )
    assert report.version==2
    again=call(dsn,read_procedure_run,actor(user),instance,command.run_id)
    assert again.steps[0].status=="USER_REPORTED_DONE"
    assert len(again.steps[0].step.instruction)==1200


def test_task_deadline_blocks_new_guided_run_but_not_late_evidence(dsn):
    user,persona,instance,conv,template=published(dsn)
    goal=make_goal(dsn,user,instance,conv)
    task,_,_=make_task(dsn,user,instance,conv,goal,1)
    source=turn(dsn,user,instance,conv,"Give this task a deadline.")
    cmd=SetTaskPriorityCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="Give this task a deadline",
        task_id=task,expected_task_version=1,
        rule="SET_DEADLINE",
        deadline=datetime.now(timezone.utc)+timedelta(days=1),
    )
    assert call(
        dsn,set_task_priority,actor(user),
        approve_task(dsn,user,instance,cmd),
    ).version==2
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].actionable

    # Simulate time passing without sleeping a day or bypassing the
    # task-domain SET_DEADLINE validation for initial configuration.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE goal_tasks SET deadline=clock_timestamp()-INTERVAL '1 day'"
            " WHERE task_id=%s AND user_id=%s AND instance_id=%s",
            (task,user,instance),
        )
    view=call(dsn,list_tasks,actor(user),instance,goal)[0]
    assert not view.actionable and "DEADLINE_EXPIRED" in view.blockers

    start=start_command(dsn,user,instance,conv,template,task=task)
    with pytest.raises(VersionConflict,match="deadline"):
        call(dsn,start_procedure_run,actor(user),start)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM procedure_runs WHERE run_id=%s",
            (start.run_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM command_receipts WHERE
               command_type='procedure.run.start.v1'
               AND idempotency_key=%s""",
            (start.idempotency_key,),
        ).fetchone()[0]==0

    # Deadline expiry must not erase real-world work or prevent late reports.
    reported=report_done(dsn,user,instance,conv,task,2)
    assert reported.action=="OUTCOME_REPORTED"
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status==(
        "USER_REPORTED_DONE"
    )
