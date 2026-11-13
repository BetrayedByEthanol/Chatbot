"""P4-04 real PostgreSQL: explicit opt-in -> ALIGN-01 -> bounded P0/P2."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.background_autonomy import (
    dispatch_one_background_step, run_autonomous_scan,
)
from persona_agent.lifecycle import (
    TrustedPlatformActor, EnterBackgroundCommand, BackgroundReason,
    PrimaryActivityCommand, enter_background, record_primary_activity,
)
from persona_agent.operation_contracts import (
    EffectClass,EnqueueOperation,Origin,Policy,Priority,Status,
)
from persona_agent.operation_scheduler import enqueue_operation
from persona_agent.operation_control import (
    cancel_operation,CancelOperationCommand,reap_stale_operations,
)
from persona_agent.procedure_agent_contracts import (
    AuthorizeBackgroundAgentStepCommand,
)
from persona_agent.procedure_agent_state import (
    authorize_background_agent_step, schedule_agent_step,
    run_one_agent_step,reconcile_agent_step,
)
from persona_agent.tool_contracts import (
    PROCEDURE_AGENT_OPERATION,RESOURCE_KEY,SLOW_TOOL,
)
from persona_agent.runtime import PermissionDenied, StaleSelection
from test_phase4_preparation import (
    _close_fixture_turns,call,drive_to_critical,drive_critical,
    advance_to_readiness,check,user_actor,
)
from test_phase3_agent_operations import staged
import test_phase3_procedures as procedure_fixtures


def producer(user):
    return TrustedPlatformActor(user_id=user,source_id="background.autonomous.dispatch")


def prepare_approval(dsn,user,instance,conv,run,*,operation_id=None,epoch=1):
    source=procedure_fixtures.turn(
        dsn,user,instance,conv,
        "Run the registered read-only step during my next Background session.",
    )
    command=AuthorizeBackgroundAgentStepCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="Run the registered read-only step",
        operation_id=operation_id or uuid4(),run_id=run,
        step_id="inspect",expected_run_version=1,
        expected_step_version=1,expected_task_version=1,
        max_words=5,expected_background_epoch=epoch,
    )
    return procedure_fixtures.approve(dsn,user,instance,command)


def ready(dsn,user,instance):
    call(dsn,enter_background,user_actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,
        idempotency_key=str(uuid4()),
    ))
    drive_to_critical(dsn,user,instance,limit=96)
    assert drive_critical(dsn,user,instance,limit=48).stage=="ALIGNMENT_PENDING"
    assert advance_to_readiness(dsn,user,instance).stage=="READY"


@pytest.fixture(autouse=True)
def cancel_other_queued_agent_operations(dsn):
    """Real cancellation, not direct mutation of the global P0 queue."""
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT operation_id,user_id,version FROM operations
               WHERE operation_type=%s AND status IN ('QUEUED','RETRY_WAIT')""",
            (PROCEDURE_AGENT_OPERATION,),
        ).fetchall()
    for op,user,version in rows:
        call(dsn,cancel_operation,user_actor(user),CancelOperationCommand(
            operation_id=op,expected_version=version,
            idempotency_key=uuid4().hex,
        ))


def test_authorized_background_readonly_step_executes_with_p0_and_preserves_task(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,goal,task,run=staged(dsn)
    approved=prepare_approval(dsn,user,instance,conv,run)
    result=call(dsn,authorize_background_agent_step,user_actor(user),approved)
    assert result.operation_id==approved.operation_id
    assert call(dsn,authorize_background_agent_step,user_actor(user),approved).replayed
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",
        (approved.operation_id,),
    )==[(0,)]
    ready(dsn,user,instance)
    admitted=call(dsn,dispatch_one_background_step,
                  producer(user),instance,1)
    assert admitted.operation_id==approved.operation_id
    assert check(dsn,
        """SELECT origin,execution_policy,priority_class,background_epoch,
                  effect_class FROM operations WHERE operation_id=%s""",
        (approved.operation_id,),
    )==[("GOAL","BACKGROUND_RESUMABLE","P2",1,"READ_ONLY")]
    assert call(dsn,dispatch_one_background_step,producer(user),instance,1) is None
    lease=call(dsn,run_one_agent_step,worker_id="p4-04-test")
    assert lease.operation_id==approved.operation_id and lease.background_epoch==1
    done=call(dsn,reconcile_agent_step,
              TrustedPlatformActor(user_id=user,source_id="p4-test-result"),
              approved.operation_id)
    assert done.resolution=="VERIFIED"
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",
        (approved.operation_id,),
    )==[("COMPLETED",)]


def test_no_background_permission_cannot_dispatch_foreground_p1_approval(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,_,task,run=staged(dsn)
    from persona_agent.procedure_agent_contracts import AuthorizeAgentStepCommand
    src=procedure_fixtures.turn(
        dsn,user,instance,conv,"Run the read-only AGENT step in this conversation.",
    )
    p1=AuthorizeAgentStepCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=src.turn_id,
        evidence_quote="Run the read-only AGENT step",
        operation_id=uuid4(),run_id=run,step_id="inspect",
        expected_run_version=1,expected_step_version=1,expected_task_version=1,
        max_words=5,
    )
    p1=procedure_fixtures.approve(dsn,user,instance,p1)
    # P3-approved Foreground steps never authorize P4 P2.
    from persona_agent.procedure_agent_state import authorize_agent_step
    call(dsn,authorize_agent_step,user_actor(user),p1)
    ready(dsn,user,instance)
    assert call(dsn,dispatch_one_background_step,producer(user),instance,1) is None
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",
        (p1.operation_id,),
    )==[(0,)]
    assert call(dsn,run_autonomous_scan)==()


def test_explicit_background_grant_cannot_be_promoted_to_foreground_p1(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,_,_,run=staged(dsn)
    granted=prepare_approval(dsn,user,instance,conv,run)
    with pytest.raises(PermissionDenied,match="Background-only"):
        call(dsn,schedule_agent_step,user_actor(user),granted)


def test_rejects_cross_epoch_and_unstaged_goal_admission(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,_,task,run=staged(dsn)
    grant=prepare_approval(dsn,user,instance,conv,run)
    call(dsn,authorize_background_agent_step,user_actor(user),grant)
    ready(dsn,user,instance)
    forged_id=uuid4()
    forged=EnqueueOperation(
        operation_id=forged_id,instance_id=instance,expected_generation=1,
        idempotency_key="procedure-agent:"+str(forged_id),
        operation_type=PROCEDURE_AGENT_OPERATION,
        origin=Origin.GOAL,execution_policy=Policy.BACKGROUND_RESUMABLE,
        priority_class=Priority.P2,effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},capabilities=(SLOW_TOOL,),
        concurrency_group="procedure.agent:"+str(forged_id),
        input_versions={"task":1,"procedure_run":1,"procedure_step":1},
        task_id=task,
    )
    # An untrusted/general platform source is barred before any DB mutation.
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,
             TrustedPlatformActor(user_id=user,source_id="arbitrary.platform"),forged)
    with pytest.raises(PermissionDenied,match="approved authorization"):
        call(dsn,enqueue_operation,producer(user),forged)
    admitted=call(dsn,dispatch_one_background_step,producer(user),instance,1)
    assert admitted.operation_id==grant.operation_id
    call(dsn,record_primary_activity,user_actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
    ))
    with pytest.raises(StaleSelection):
        call(dsn,dispatch_one_background_step,producer(user),instance,1)
    # The old queued P2 operation must not carry permission to the next epoch.
    assert check(dsn,
        "SELECT background_epoch FROM operations WHERE operation_id=%s",
        (grant.operation_id,),
    )==[(1,)]


def test_foreground_return_during_readonly_tool_fences_p2_result(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,_,task,run=staged(dsn)
    approved=prepare_approval(dsn,user,instance,conv,run)
    call(dsn,authorize_background_agent_step,user_actor(user),approved)
    ready(dsn,user,instance)
    call(dsn,dispatch_one_background_step,producer(user),instance,1)
    def interrupt(instruction,max_words):
        call(dsn,record_primary_activity,user_actor(user),PrimaryActivityCommand(
            instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        ))
        return "the read-only operation finished"
    lease=call(dsn,run_one_agent_step,worker_id="p4-04-race",executor=interrupt)
    assert lease.operation_id==approved.operation_id
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(approved.operation_id,),
    )==[("RUNNING",)]
    assert check(dsn,
        "SELECT status FROM procedure_run_steps WHERE run_id=%s AND step_id='inspect'",
        (run,),
    )==[("NOT_STARTED",)]
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert call(dsn,run_autonomous_scan)==()


def test_ready_epoch_scanner_does_not_starve_later_personas(dsn):
    from test_phase4_preparation import seed,tick,advance_to_readiness

    # No new permissions are issued, and no tool executes. This isolates the
    # durable READY-poll cursor from the separate P0 admission system.
    isolated=[]
    for _ in range(4):
        user,instance,_=seed(dsn)
        call(dsn,enter_background,user_actor(user),EnterBackgroundCommand(
            instance_id=instance,expected_generation=1,
            expected_presence_revision=0,
            reason=BackgroundReason.EXPLICIT_DEPARTURE,
            idempotency_key=uuid4().hex,
        ))
        assert tick(dsn,user,instance).stage=="CRITICAL_PENDING"
        assert tick(dsn,user,instance).stage=="ALIGNMENT_PENDING"
        assert advance_to_readiness(dsn,user,instance).stage=="READY"
        isolated.append((user,instance))

    count=check(dsn,
        """SELECT count(*) FROM background_preparation_epochs e
           JOIN persona_selection s ON s.user_id=e.user_id
             AND s.instance_id=e.instance_id
             AND s.background_epoch=e.background_epoch
           WHERE s.mode='BACKGROUND' AND e.status='PENDING'
             AND e.preparation_stage='READY'""",())[0][0]
    for _ in range(count+4):
        assert call(dsn,run_autonomous_scan,limit=1)==()
    for user,instance in isolated:
        assert check(dsn,
            """SELECT autonomous_polled_at IS NOT NULL
               FROM background_preparation_epochs WHERE user_id=%s
                 AND instance_id=%s AND background_epoch=1""",
            (user,instance),
        )==[(True,)]
