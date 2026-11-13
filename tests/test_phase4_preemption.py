"""P4-05a: real PostgreSQL cooperative Background suspension acceptance."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.lifecycle import (
    PrimaryActivityCommand,TrustedPlatformActor,record_primary_activity,
)
from persona_agent.operation_contracts import StaleOperation
from persona_agent.operation_control import (
    acknowledge_suspend,request_background_suspension,reap_stale_operations,
    heartbeat_operation,complete_operation,
)
from persona_agent.operation_scheduler import claim_operation
from persona_agent.procedure_agent_state import (
    authorize_background_agent_step,run_one_agent_step,
)
from persona_agent.background_autonomy import dispatch_one_background_step
from persona_agent.tool_contracts import PROCEDURE_AGENT_OPERATION,SLOW_TOOL
from persona_agent.runtime import PermissionDenied
from test_phase3_agent_operations import staged
from test_phase4_autonomy import ready,prepare_approval,producer
from test_phase4_preparation import call,check,user_actor,_close_fixture_turns


def _admitted(dsn,monkeypatch):
    _close_fixture_turns(dsn,monkeypatch)
    user,instance,conv,goal,task,run=staged(dsn)
    approval=prepare_approval(dsn,user,instance,conv,run)
    call(dsn,authorize_background_agent_step,user_actor(user),approval)
    ready(dsn,user,instance)
    call(dsn,dispatch_one_background_step,producer(user),instance,1)
    return user,instance,task,run,approval.operation_id


def _return(dsn,user,instance):
    return call(dsn,record_primary_activity,user_actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
    ))


def _lease(dsn):
    return call(
        dsn,claim_operation,worker_id=f"preempt-{uuid4().hex[:8]}",
        capabilities=(SLOW_TOOL,),operation_types=(PROCEDURE_AGENT_OPERATION,),
        lease_seconds=120,
    )


def test_foreground_return_signals_running_p2_then_worker_acknowledges(dsn,monkeypatch):
    user,instance,task,run,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    assert lease is not None and lease.operation_id==op
    returned=_return(dsn,user,instance)
    assert returned.mode.value=="FOREGROUND"
    # Unlike a forced reaper transition, RUNNING retains its resources until
    # worker acknowledgment or expiration. The lifecycle update has committed.
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("RUNNING",)]
    preemptor=TrustedPlatformActor(user_id=user,source_id="background.preemption")
    assert call(dsn,request_background_suspension,preemptor,op) is True
    assert call(dsn,request_background_suspension,preemptor,op) is False
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("SUSPEND_REQUESTED",)]
    with pytest.raises(StaleOperation):
        call(dsn,complete_operation,lease,result_ref="rejected")
    call(dsn,acknowledge_suspend,lease)
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(op,),
    )==[("SUSPENDED",)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        "SELECT status FROM procedure_run_steps WHERE run_id=%s AND step_id='inspect'",
        (run,),
    )==[("NOT_STARTED",)]
    with pytest.raises(StaleOperation):
        call(dsn,acknowledge_suspend,lease)
    assert check(dsn,
        """SELECT count(*) FROM events
           WHERE event_type='operation.suspend_requested.v1'
             AND payload->>'operation_id'=%s""",(str(op),),
    )==[(1,)]


def test_generic_reaper_requests_cooperation_before_lease_expiry(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    _return(dsn,user,instance)
    assert call(dsn,reap_stale_operations)>=1
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("SUSPEND_REQUESTED",)]
    # A second sweep must not declare an actively executing tool suspended.
    call(dsn,reap_stale_operations)
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("SUSPEND_REQUESTED",)]
    call(dsn,acknowledge_suspend,lease)


def test_synchronous_readonly_tool_disposes_result_after_foreground_return(dsn,monkeypatch):
    user,instance,task,run,op=_admitted(dsn,monkeypatch)
    def return_inside_tool(text,max_words):
        _return(dsn,user,instance)
        return "untrusted after-return result"
    lease=call(dsn,run_one_agent_step,worker_id=f"stop-{uuid4().hex[:8]}",
               executor=return_inside_tool)
    assert lease is not None and lease.operation_id==op
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("SUSPENDED",)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT status FROM procedure_run_steps WHERE run_id=%s AND step_id='inspect'",
        (run,),
    )==[("NOT_STARTED",)]


def test_preemption_requires_platform_scope_and_revocation(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    preemptor=TrustedPlatformActor(user_id=user,source_id="background.preemption")
    assert call(dsn,request_background_suspension,preemptor,op) is False
    with pytest.raises(PermissionDenied):
        call(dsn,request_background_suspension,
             TrustedPlatformActor(user_id=uuid4(),source_id="background.preemption"),op)
    with pytest.raises(PermissionDenied):
        call(dsn,request_background_suspension,
             TrustedPlatformActor(user_id=user,source_id="arbitrary.platform"),op)
    _return(dsn,user,instance)
    assert call(dsn,request_background_suspension,preemptor,op)
    call(dsn,acknowledge_suspend,lease)
