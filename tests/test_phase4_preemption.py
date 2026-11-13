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
    complete_operation,cancel_operation,acknowledge_cancel,CancelOperationCommand,
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
    # PostgreSQL acceptance intentionally shares one session database.
    # Older tests may leave a registered step in RETRY_WAIT (including a
    # previously expired claim). The global P0 worker can otherwise claim
    # that older eligible step instead of the one this test just admitted.
    # Retire only unclaimed test steps through the real authenticated API.
    with psycopg.connect(dsn,autocommit=True) as conn:
        pending=conn.execute(
            """SELECT operation_id,user_id,version FROM operations
               WHERE operation_type=%s AND status IN ('QUEUED','RETRY_WAIT')""",
            (PROCEDURE_AGENT_OPERATION,),
        ).fetchall()
    for operation_id,prior_user,version in pending:
        call(dsn,cancel_operation,user_actor(prior_user),CancelOperationCommand(
            operation_id=operation_id,expected_version=version,
            idempotency_key=uuid4().hex,
        ))
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


def test_user_cancellation_takes_precedence_over_pending_p2_suspension(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    _return(dsn,user,instance)
    preemptor=TrustedPlatformActor(user_id=user,source_id="background.preemption")
    assert call(dsn,request_background_suspension,preemptor,op)
    version=check(dsn,"SELECT version FROM operations WHERE operation_id=%s",(op,))[0][0]
    status=call(dsn,cancel_operation,user_actor(user),CancelOperationCommand(
        operation_id=op,expected_version=version,idempotency_key=uuid4().hex,
    ))
    assert status.value=="CANCEL_REQUESTED"
    call(dsn,reap_stale_operations)
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("CANCEL_REQUESTED",)]
    with pytest.raises(StaleOperation):
        call(dsn,acknowledge_suspend,lease)
    call(dsn,acknowledge_cancel,lease)
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(op,),
    )==[("CANCELLED",)]


def test_dispatch_rechecks_lease_expiry_after_preemption_lock_wait(dsn,monkeypatch):
    """The P2 executor must not run on a claim expired after first gate."""
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    import persona_agent.procedure_agent_state as agent
    old_request=agent.request_background_suspension
    once=[False]

    def delay_request(connection,actor,operation_id):
        response=old_request(connection,actor,operation_id)
        if not once[0]:
            once[0]=True
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute(
                    """UPDATE operations
                       SET lease_until=clock_timestamp()-interval '1 second'
                       WHERE operation_id=%s""",(op,),
                )
        return response

    monkeypatch.setattr(agent,"request_background_suspension",delay_request)
    invoked=[]
    lease=call(dsn,run_one_agent_step,worker_id="p405-lease-race",
               executor=lambda instruction,words: invoked.append(instruction) or "wrong")
    assert lease is not None and lease.operation_id==op
    assert once[0] and not invoked
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("RUNNING",)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert call(dsn,reap_stale_operations)>=1


def test_dispatch_rechecks_task_deadline_after_preemption_lock_wait(dsn,monkeypatch):
    """Task deadline can elapse after preemption's selection/row lock waits."""
    user,instance,task,_,op=_admitted(dsn,monkeypatch)
    import persona_agent.procedure_agent_state as agent
    old_request=agent.request_background_suspension
    once=[False]

    def delay_request(connection,actor,operation_id):
        response=old_request(connection,actor,operation_id)
        if not once[0]:
            once[0]=True
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute(
                    """UPDATE goal_tasks
                       SET deadline=clock_timestamp()-interval '1 second'
                       WHERE task_id=%s""",(task,),
                )
        return response

    monkeypatch.setattr(agent,"request_background_suspension",delay_request)
    invoked=[]
    lease=call(dsn,run_one_agent_step,worker_id="p405-deadline-race",
               executor=lambda instruction,words: invoked.append(instruction) or "wrong")
    assert lease is not None and lease.operation_id==op
    assert once[0] and not invoked
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]


def test_cancellation_race_after_suspension_read_gets_acknowledged(dsn,monkeypatch):
    """User cancellation between SELECT and ACK must release live P2 slots."""
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    import persona_agent.procedure_agent_state as agent
    real_ack=agent.acknowledge_suspend
    intervened=[False]

    def cancellation_wins(connection,lease):
        if not intervened[0]:
            intervened[0]=True
            version=check(dsn,
                "SELECT version FROM operations WHERE operation_id=%s",(op,),
            )[0][0]
            status=call(dsn,cancel_operation,user_actor(user),CancelOperationCommand(
                operation_id=op,expected_version=version,idempotency_key=uuid4().hex,
            ))
            assert status.value=="CANCEL_REQUESTED"
        return real_ack(connection,lease)

    monkeypatch.setattr(agent,"acknowledge_suspend",cancellation_wins)

    def return_during_execution(instruction,words):
        _return(dsn,user,instance)
        return "must be discarded"

    lease=call(dsn,run_one_agent_step,worker_id="p405-ack-race",
               executor=return_during_execution)
    assert lease is not None and lease.operation_id==op and intervened[0]
    assert check(dsn,"SELECT status FROM operations WHERE operation_id=%s",(op,))==[("CANCELLED",)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]


def test_failure_reporting_losing_preemption_race_settles_live_claim(dsn,monkeypatch):
    """The worker stops after failure, so it must ACK a newly revoked epoch."""
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    import persona_agent.procedure_agent_state as agent
    real_fail=agent.fail_operation
    intervened=[False]

    def revocation_wins(connection,lease,*,reason,delay_seconds=1):
        if not intervened[0]:
            intervened[0]=True
            _return(dsn,user,instance)
        return real_fail(connection,lease,reason=reason,delay_seconds=delay_seconds)

    monkeypatch.setattr(agent,"fail_operation",revocation_wins)

    def broken_tool(instruction,words):
        raise RuntimeError("simulated tool error")

    lease=call(dsn,run_one_agent_step,worker_id="p405-failure-race",
               executor=broken_tool)
    assert lease is not None and lease.operation_id==op and intervened[0]
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(op,),
    )==[("SUSPENDED",)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]
