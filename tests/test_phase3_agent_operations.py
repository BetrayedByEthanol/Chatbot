"""P3-04 AGENT operation vertical slice: P0 scheduler + scoped read-only tool."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.operation_contracts import (
    EffectClass, EnqueueOperation, Policy, Priority, StaleOperation,
)
from persona_agent.operation_scheduler import (
    claim_operation, configure_resource_pool, enqueue_operation,
)
from persona_agent.operation_control import (
    CancelOperationCommand, cancel_operation,
)
from persona_agent.procedure_contracts import (
    ProcedureDefinition, ProcedureStep,
)
from persona_agent.procedure_agent_contracts import AuthorizeAgentStepCommand
from persona_agent.procedure_agent_state import (
    authorize_agent_step, reconcile_agent_step, run_one_agent_step,
    schedule_agent_step,
)
from persona_agent.procedure_run_state import (
    read_procedure_run, start_procedure_run,
)
from persona_agent.runtime import PermissionDenied, VersionConflict
from persona_agent.task_state import list_tasks
from persona_agent.tool_contracts import (
    PROCEDURE_AGENT_OPERATION, SLOW_TOOL, RESOURCE_KEY,
)

from test_phase3_procedures import actor, approve, call, turn
from test_phase3_runs import published, run_status, start_command
from test_phase3_tasks import make_goal, make_task, priority


def configured_pool(dsn):
    call(dsn,configure_resource_pool,RESOURCE_KEY,
         capacity_units=8,p0_reserved_units=0)


def agent_method(capability=SLOW_TOOL):
    return ProcedureDefinition(
        title="Analyze a procedure instruction",
        purpose="Run bounded registered read-only text analysis",
        applicability="Local non-sensitive demonstration data",
        limitations="No browsing or external side effects",
        policy="STABLE",preconditions=("Approved task",),
        steps=(ProcedureStep(
            step_id="inspect",instruction="Analyze the local test input and count words",
            executor="AGENT",capability=capability,
            success_test="Registered read-only operation has a trusted result",
        ),),
        success_tests=("Operation result has claim-generation provenance",),
    )


def staged(dsn,*,configured=True,capability=SLOW_TOOL):
    user,persona,instance,conv,template=published(
        dsn,defined=agent_method(capability),
    )
    if configured:
        configured_pool(dsn)
    goal=make_goal(dsn,user,instance,conv)
    task,_,_=make_task(dsn,user,instance,conv,goal,1,
                       executor="AGENT",capability=capability)
    command=start_command(dsn,user,instance,conv,template,task=task)
    call(dsn,start_procedure_run,actor(user),command)
    return user,instance,conv,goal,task,command.run_id


def authorization(dsn,user,instance,conv,run,*,task_version=1,
                  run_version=1,step_version=1,operation_id=None):
    evidence=turn(dsn,user,instance,conv,
                  "Please execute this registered read-only AGENT step.")
    request=AuthorizeAgentStepCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=evidence.turn_id,
        evidence_quote="Please execute this registered read-only AGENT step",
        operation_id=operation_id or uuid4(),
        run_id=run,expected_run_version=run_version,
        step_id="inspect",expected_step_version=step_version,
        expected_task_version=task_version,max_words=5,
    )
    return approve(dsn,user,instance,request)


def platform(user):
    return TrustedPlatformActor(
        user_id=user,source_id="procedure.agent.test.reconciler",
    )


def operation_row(dsn,operation_id):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(
            """SELECT status,effect_status,task_id,claim_generation,
                      result_ref,version FROM operations WHERE operation_id=%s""",
            (operation_id,),
        ).fetchone()


def test_agent_step_executes_only_with_versioned_explicit_user_approval(dsn):
    user,instance,conv,goal,task,run=staged(dsn)
    cmd=authorization(dsn,user,instance,conv,run)
    admitted=call(dsn,schedule_agent_step,actor(user),cmd)
    assert admitted.status.value=="QUEUED"
    assert call(dsn,schedule_agent_step,actor(user),cmd).replayed
    assert operation_row(dsn,cmd.operation_id)[2]==task
    view=call(dsn,read_procedure_run,actor(user),instance,run)
    assert view.steps[0].status=="NOT_STARTED"
    assert not view.operation_dispatch_authorized

    lease=call(dsn,run_one_agent_step,worker_id="agent-worker-1")
    assert lease is not None and lease.operation_id==cmd.operation_id
    row=operation_row(dsn,cmd.operation_id)
    assert row[0]=="COMPLETED" and row[1]=="CONFIRMED"
    accepted=call(dsn,reconcile_agent_step,platform(user),cmd.operation_id)
    assert accepted.resolution=="VERIFIED"
    assert accepted.step_event_id is not None
    replay=call(dsn,reconcile_agent_step,platform(user),cmd.operation_id)
    assert replay.replayed and replay.resolution=="VERIFIED"
    state=call(dsn,read_procedure_run,actor(user),instance,run)
    assert state.steps[0].status=="VERIFIED"
    assert state.next_step_id is None
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status=="PLANNED"
    final=run_status(dsn,user,instance,conv,run,state.version,"FINISH")
    assert final.action=="RUN_USER_CONFIRMED"
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status=="PLANNED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT e.trust_level FROM procedure_run_step_events se
               JOIN events e ON e.event_id=se.event_id
               WHERE se.step_event_id=%s""",
            (accepted.step_event_id,),
        ).fetchone()
        assert row==("TRUSTED_PLATFORM",)


def test_forged_task_owned_enqueue_is_denied_without_staged_grant(dsn):
    user,instance,conv,goal,task,run=staged(dsn)
    with pytest.raises(ValueError,match="registered procedure gate"):
        EnqueueOperation(
            operation_id=uuid4(),instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,operation_type="arbitrary.code.exec",
            resources={RESOURCE_KEY:1},task_id=task,
        )
    operation_id=uuid4()
    cmd=EnqueueOperation(
        operation_id=operation_id,instance_id=instance,
        expected_generation=1,
        idempotency_key=f"procedure-agent:{operation_id}",
        operation_type=PROCEDURE_AGENT_OPERATION,
        execution_policy=Policy.FOREGROUND_ASYNC,
        priority_class=Priority.P1,effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},capabilities=(SLOW_TOOL,),
        concurrency_group=f"procedure.agent:{operation_id}",
        input_versions={"task":1,"procedure_run":1,"procedure_step":1},
        task_id=task,
    )
    with pytest.raises(PermissionDenied,match="authorization"):
        call(dsn,enqueue_operation,actor(user),cmd)
    assert operation_row(dsn,operation_id) is None


def test_nonregistered_or_mismatched_capability_cannot_be_authorized(dsn):
    user,instance,conv,goal,task,run=staged(
        dsn,capability="browser.search",
    )
    cmd=authorization(dsn,user,instance,conv,run)
    with pytest.raises(PermissionDenied,match="registered read-only"):
        call(dsn,authorize_agent_step,actor(user),cmd)
    assert operation_row(dsn,cmd.operation_id) is None


def test_task_priority_change_revokes_queued_operation_and_reconciliation(dsn):
    user,instance,conv,goal,task,run=staged(dsn)
    cmd=authorization(dsn,user,instance,conv,run)
    call(dsn,schedule_agent_step,actor(user),cmd)
    changed=priority(dsn,user,instance,conv,task,1,"SUSPEND")
    assert changed.version==2
    assert call(dsn,claim_operation,worker_id="invalid-claim",
                capabilities=(SLOW_TOOL,),
                operation_types=(PROCEDURE_AGENT_OPERATION,)) is None
    assert operation_row(dsn,cmd.operation_id)[0]=="QUEUED"
    # Existing P0 user cancellation closes the outstanding operation.
    status=call(dsn,cancel_operation,actor(user),CancelOperationCommand(
        operation_id=cmd.operation_id,expected_version=0,
        idempotency_key=uuid4().hex,
    ))
    assert status.value=="CANCELLED"
    result=call(dsn,reconcile_agent_step,platform(user),cmd.operation_id)
    assert result.resolution=="INVALIDATED"
    assert call(dsn,read_procedure_run,actor(user),instance,run
                ).steps[0].status=="NOT_STARTED"
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status=="PLANNED"


def test_cancellation_during_worker_prevents_fenced_completion(dsn):
    user,instance,conv,goal,task,run=staged(dsn)
    cmd=authorization(dsn,user,instance,conv,run)
    call(dsn,schedule_agent_step,actor(user),cmd)
    def cancel_mid_tool(text,max_words):
        with psycopg.connect(dsn,autocommit=True) as conn:
            row=conn.execute(
                "SELECT version FROM operations WHERE operation_id=%s",
                (cmd.operation_id,),
            ).fetchone()
        assert row is not None
        call(dsn,cancel_operation,actor(user),CancelOperationCommand(
            operation_id=cmd.operation_id,expected_version=row[0],
            idempotency_key=uuid4().hex,
        ))
        return "Text analysis: fake result is never accepted."
    lease=call(dsn,run_one_agent_step,worker_id="cancel-worker",
               executor=cancel_mid_tool)
    assert lease is not None and lease.operation_id==cmd.operation_id
    assert operation_row(dsn,cmd.operation_id)[0]=="CANCEL_REQUESTED"
    # An unacknowledged pending cancellation cannot pass COMPLETED or VERIFIED.
    with pytest.raises(VersionConflict,match="not terminal"):
        call(dsn,reconcile_agent_step,platform(user),cmd.operation_id)
    assert call(dsn,read_procedure_run,actor(user),instance,run
                ).steps[0].status=="NOT_STARTED"


def test_completed_operation_is_not_accepted_after_run_version_change(dsn):
    user,instance,conv,goal,task,run=staged(dsn)
    cmd=authorization(dsn,user,instance,conv,run)
    call(dsn,schedule_agent_step,actor(user),cmd)
    call(dsn,run_one_agent_step,worker_id="late-reconcile-worker")
    assert operation_row(dsn,cmd.operation_id)[0]=="COMPLETED"
    # A new user-approved pause invalidates original run input versions.
    run_status(dsn,user,instance,conv,run,1,"PAUSE")
    result=call(dsn,reconcile_agent_step,platform(user),cmd.operation_id)
    assert result.resolution=="INVALIDATED"
    assert call(dsn,read_procedure_run,actor(user),instance,run
                ).steps[0].status=="NOT_STARTED"
