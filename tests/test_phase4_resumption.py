"""P4-05c: real PostgreSQL explicit cross-epoch read-only resumption."""
from dataclasses import replace
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.lifecycle import (
    EnterBackgroundCommand,BackgroundReason,enter_background,
)
from persona_agent.procedure_agent_contracts import RenewBackgroundAgentStepCommand
from persona_agent.procedure_agent_state import (
    renew_background_agent_step,run_one_agent_step,reconcile_agent_step,
    authorize_background_agent_step,
)
from persona_agent.background_autonomy import dispatch_one_background_step
from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.runtime import PermissionDenied,StaleSelection,VersionConflict
from persona_agent.operation_control import CancelOperationCommand,cancel_operation
from persona_agent.procedure_agent_checkpoint import load_validated_procedure_checkpoint
from test_phase4_checkpoints import _checkpointed
from test_phase4_autonomy import producer
from test_phase4_preparation import call,check,user_actor,drive_to_critical,drive_critical,advance_to_readiness
import test_phase3_procedures as fixture


def _stopped(dsn,monkeypatch):
    user,instance,task,run,old,lease=_checkpointed(dsn,monkeypatch)
    ref=check(dsn,"SELECT checkpoint_ref FROM operations WHERE operation_id=%s",(old,))[0][0]
    assert ref==f"procedure-step:v1:{old}:{lease.claim_generation}"
    return user,instance,task,run,old,ref


def _renew_command(dsn,user,instance,run,source,checkpoint_ref,*,epoch=3,
                   new_operation_id=None,max_words=5):
    # A fresh evidence turn and a separately approved turn are mandatory;
    # the full command payload (including prior checkpoint) is in the phrase.
    conv=check(dsn,
        "SELECT source_conversation_id FROM procedure_runs WHERE run_id=%s",(run,),
    )[0][0]
    evidence=fixture.turn(dsn,user,instance,conv,
        "I authorize restarting this stopped read-only step in my next Background session.")
    command=RenewBackgroundAgentStepCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=evidence.turn_id,
        evidence_quote="I authorize restarting this stopped read-only step",
        operation_id=new_operation_id or uuid4(),
        run_id=run,step_id="inspect",
        expected_run_version=1,expected_step_version=1,expected_task_version=1,
        max_words=max_words,expected_background_epoch=epoch,
        resume_from_operation_id=source,resume_checkpoint_ref=checkpoint_ref,
    )
    return fixture.approve(dsn,user,instance,command)


def _enter_next_ready(dsn,user,instance):
    presence=check(dsn,
        "SELECT presence_revision FROM persona_selection WHERE user_id=%s",
        (user,),
    )[0][0]
    result=call(dsn,enter_background,user_actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,
        expected_presence_revision=presence,reason=BackgroundReason.EXPLICIT_DEPARTURE,
        idempotency_key=uuid4().hex,
    ))
    assert result.background_epoch==3
    drive_to_critical(dsn,user,instance,limit=96)
    assert drive_critical(dsn,user,instance,limit=48).stage=="ALIGNMENT_PENDING"
    assert advance_to_readiness(dsn,user,instance).stage=="READY"


def test_explicit_renewal_restarts_from_checkpoint_in_next_ready_epoch(dsn,monkeypatch):
    user,instance,task,run,old,ref=_stopped(dsn,monkeypatch)
    restored=call(dsn,load_validated_procedure_checkpoint,user_actor(user),
                  operation_id=old)
    command=_renew_command(dsn,user,instance,run,old,ref)
    approved=call(dsn,renew_background_agent_step,user_actor(user),command)
    assert approved.operation_id==command.operation_id
    assert call(dsn,renew_background_agent_step,user_actor(user),command).replayed
    assert check(dsn,
        "SELECT status,checkpoint_ref FROM operations WHERE operation_id=%s",(old,),
    )==[("SUSPENDED",ref)]
    assert check(dsn,
        """SELECT state,approved_background_epoch
           FROM procedure_agent_operation_authorizations WHERE operation_id=%s""",
        (old,),
    )==[("INVALIDATED",1)]
    assert check(dsn,
        """SELECT source_operation_id,checkpoint_ref,approved_background_epoch
           FROM procedure_agent_resume_links WHERE operation_id=%s""",
        (command.operation_id,),
    )==[(old,ref,3)]
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",(command.operation_id,),
    )==[(0,)]
    # User approval is not dispatch; P0 autonomy still requires a READY epoch.
    assert call(dsn,dispatch_one_background_step,producer(user),instance,3) is None
    _enter_next_ready(dsn,user,instance)
    dispatched=call(dsn,dispatch_one_background_step,producer(user),instance,3)
    assert dispatched is not None and dispatched.operation_id==command.operation_id
    observed=[]
    def executor(instruction,max_words):
        observed.append((instruction,max_words))
        return "validated resumed read-only result"
    lease=call(dsn,run_one_agent_step,worker_id=f"p405c-{uuid4().hex[:8]}",
               executor=executor)
    assert lease is not None and lease.operation_id==command.operation_id
    assert lease.background_epoch==3
    assert observed==[(restored.instruction,restored.max_words)]
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(command.operation_id,),
    )==[("COMPLETED",)]
    settled=call(dsn,reconcile_agent_step,
                 TrustedPlatformActor(user_id=user,source_id="background.autonomous.result"),
                 command.operation_id)
    assert settled.resolution=="VERIFIED"
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT count(*) FROM events WHERE event_type='procedure.agent.background_resumption_authorized.v1' "
        "AND payload->>'operation_id'=%s",(str(command.operation_id),),
    )==[(1,)]


def test_no_renewal_without_exact_safe_stop_source(dsn,monkeypatch):
    user,instance,_,run,old,ref=_stopped(dsn,monkeypatch)
    wrong_ref=_renew_command(dsn,user,instance,run,old,ref+":tampered")
    with pytest.raises(VersionConflict):
        call(dsn,renew_background_agent_step,user_actor(user),wrong_ref)
    changed_parameters=_renew_command(dsn,user,instance,run,old,ref,max_words=8)
    with pytest.raises(VersionConflict):
        call(dsn,renew_background_agent_step,user_actor(user),changed_parameters)
    assert check(dsn,
        "SELECT state FROM procedure_agent_operation_authorizations WHERE operation_id=%s",
        (old,),
    )==[("AUTHORIZED",)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_resume_links WHERE source_operation_id=%s",
        (old,),
    )==[(0,)]


def test_next_epoch_only_and_cross_user_cannot_renew(dsn,monkeypatch):
    user,instance,_,run,old,ref=_stopped(dsn,monkeypatch)
    wrong_epoch=_renew_command(dsn,user,instance,run,old,ref,epoch=5)
    with pytest.raises(StaleSelection):
        call(dsn,renew_background_agent_step,user_actor(user),wrong_epoch)
    valid=_renew_command(dsn,user,instance,run,old,ref)
    with pytest.raises(PermissionDenied):
        call(dsn,renew_background_agent_step,user_actor(uuid4()),valid)
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_resume_links WHERE source_operation_id=%s",
        (old,),
    )==[(0,)]


def test_does_not_reuse_suspended_operation_or_automatically_resume(dsn,monkeypatch):
    user,instance,_,run,old,ref=_stopped(dsn,monkeypatch)
    same_id=_renew_command(dsn,user,instance,run,old,ref,new_operation_id=old)
    with pytest.raises(PermissionDenied):
        call(dsn,renew_background_agent_step,user_actor(user),same_id)
    approved=_renew_command(dsn,user,instance,run,old,ref)
    call(dsn,renew_background_agent_step,user_actor(user),approved)
    assert check(dsn,
        "SELECT attempt_count,status FROM operations WHERE operation_id=%s",(old,),
    )==[(1,"SUSPENDED")]
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",(approved.operation_id,),
    )==[(0,)]


def test_source_cancellation_revokes_renewed_lineage_at_dispatch(dsn,monkeypatch):
    user,instance,_,run,old,ref=_stopped(dsn,monkeypatch)
    approved=_renew_command(dsn,user,instance,run,old,ref)
    call(dsn,renew_background_agent_step,user_actor(user),approved)
    version=check(dsn,"SELECT version FROM operations WHERE operation_id=%s",(old,))[0][0]
    assert call(dsn,cancel_operation,user_actor(user),CancelOperationCommand(
        operation_id=old,expected_version=version,idempotency_key=uuid4().hex,
    )).value=="CANCELLED"
    _enter_next_ready(dsn,user,instance)
    with pytest.raises((VersionConflict,PermissionDenied)):
        call(dsn,dispatch_one_background_step,producer(user),instance,3)
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",(approved.operation_id,),
    )==[(0,)]
