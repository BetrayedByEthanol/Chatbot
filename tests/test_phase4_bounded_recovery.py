"""P4-05d: real PostgreSQL bounded P2 read-only recovery contracts.

No special worker/scheduler: exercise the P0 queue, claim, lease, reaper,
Procedure safe-stop and real user authorization paths.
"""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.operation_contracts import InvalidOperation, StaleOperation, Status
from persona_agent.operation_control import (
    checkpoint_operation,complete_operation,reap_invalid_background_queue,
    reap_stale_operations,
)
from persona_agent.procedure_agent_state import (
    authorize_background_agent_step,reconcile_agent_step,run_one_agent_step,
)
from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.runtime import VersionConflict
from test_phase4_preemption import _admitted,_lease,_return
from test_phase4_checkpoints import _checkpointed
from test_phase4_autonomy import prepare_approval
from test_phase4_preparation import call,check,user_actor


def _row(dsn,operation_id):
    return check(dsn,
        """SELECT status,attempt_count,claim_generation,
                  checkpoint_ref,eligible_at,updated_at
           FROM operations WHERE operation_id=%s""",(operation_id,),
    )[0]


def _resources(dsn,operation_id):
    return check(dsn,
        """SELECT count(*) FROM operation_resource_reservations
           WHERE operation_id=%s""",(operation_id,),
    )[0][0]


def _tool_failure(text,max_words):
    raise RuntimeError("simulated bounded read-only failure")


def test_registered_p2_retries_with_p0_backoff_then_fails_terminally(dsn,monkeypatch):
    user,instance,task,run,op=_admitted(dsn,monkeypatch)
    for attempt,delay in ((1,2),(2,4),(3,None)):
        lease=call(dsn,run_one_agent_step,
                   worker_id=f"p405d-fail-{attempt}-{uuid4().hex[:6]}",
                   executor=_tool_failure)
        assert lease is not None and lease.operation_id==op
        status,attempt_count,generation,checkpoint_ref,eligible,updated=_row(dsn,op)
        assert attempt_count==attempt and generation==attempt
        assert checkpoint_ref is None
        assert _resources(dsn,op)==0
        if delay is not None:
            assert status=="RETRY_WAIT"
            assert (eligible-updated).total_seconds()==pytest.approx(delay,abs=0.25)
            # The normal scheduler must honor retry eligibility and cannot
            # bypass the backoff even with another worker ready.
            assert call(dsn,run_one_agent_step,
                        worker_id=f"p405d-early-{attempt}",
                        executor=_tool_failure) is None
            # Advance ONLY test time eligibility (not attempt_count/status).
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute(
                    """UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second'
                       WHERE operation_id=%s""",(op,),
                )
        else:
            assert status=="FAILED"
            assert call(dsn,run_one_agent_step,
                        worker_id="p405d-after-exhaustion",
                        executor=_tool_failure) is None
    assert check(dsn,
        """SELECT count(*) FROM events
           WHERE event_type='operation.retry_wait.v1'
             AND payload->>'operation_id'=%s""",(str(op),),
    )==[(2,)]
    assert check(dsn,
        """SELECT count(*) FROM events
           WHERE event_type='operation.failed.v1'
             AND payload->>'operation_id'=%s""",(str(op),),
    )==[(1,)]
    settled=call(dsn,reconcile_agent_step,
        TrustedPlatformActor(user_id=user,source_id="background.autonomous.result"),op)
    assert settled.resolution=="FAILED"
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]


def test_expired_claim_retry_is_fenced_and_does_not_reuse_old_tool_result(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    old=_lease(dsn)
    assert old is not None and old.operation_id==op
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    assert call(dsn,reap_stale_operations)>=1
    status,attempt,generation,checkpoint_ref,eligible,updated=_row(dsn,op)
    assert (status,attempt,generation,checkpoint_ref)==("RETRY_WAIT",1,1,None)
    assert (eligible-updated).total_seconds()==pytest.approx(2,abs=0.25)
    assert _resources(dsn,op)==0
    with pytest.raises(StaleOperation):
        call(dsn,complete_operation,old,result_ref="stale-success")
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    renewed=call(dsn,run_one_agent_step,worker_id=f"p405d-retry-{uuid4().hex[:8]}",
                 executor=lambda text,words:"read-only recovered")
    assert renewed is not None and renewed.operation_id==op
    assert renewed.claim_generation==2
    assert _row(dsn,op)[0]=="COMPLETED"
    assert _resources(dsn,op)==0


def test_crashed_p2_after_return_is_retired_without_fabricating_checkpoint(dsn,monkeypatch):
    user,instance,_,run,op=_admitted(dsn,monkeypatch)
    old=_lease(dsn)
    _return(dsn,user,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert _row(dsn,op)[:4]==("SUSPENDED",1,1,None)
    assert _resources(dsn,op)==0
    # The P4-05c loader correctly cannot restore this orphan. Instead of
    # leaving an unreplaceable AUTHORIZED stage forever, P0 blocks it safely.
    assert call(dsn,reap_invalid_background_queue)>=1
    assert _row(dsn,op)[0]=="BLOCKED"
    assert call(dsn,reap_invalid_background_queue)==0
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_checkpoints WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        """SELECT count(*) FROM events
           WHERE event_type='operation.blocked.v1'
             AND payload->>'operation_id'=%s""",(str(op),),
    )==[(1,)]
    conv=check(dsn,
        "SELECT source_conversation_id FROM procedure_runs WHERE run_id=%s",(run,),
    )[0][0]
    fresh=prepare_approval(dsn,user,instance,conv,run,epoch=3)
    approved=call(dsn,authorize_background_agent_step,user_actor(user),fresh)
    assert approved.operation_id==fresh.operation_id
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",
        (fresh.operation_id,),
    )==[(0,)]


def test_immutable_safe_stop_checkpoint_survives_recovery_sweeps(dsn,monkeypatch):
    user,instance,task,run,op,lease=_checkpointed(dsn,monkeypatch)
    ref=_row(dsn,op)[3]
    assert ref==f"procedure-step:v1:{op}:{lease.claim_generation}"
    call(dsn,reap_invalid_background_queue)
    call(dsn,reap_stale_operations)
    assert _row(dsn,op)[0]=="SUSPENDED"
    assert _row(dsn,op)[3]==ref
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_checkpoints WHERE operation_id=%s",
        (op,),
    )==[(1,)]
    assert check(dsn,
        "SELECT state FROM procedure_agent_operation_authorizations WHERE operation_id=%s",
        (op,),
    )==[("AUTHORIZED",)]


def test_opaque_p0_checkpoint_cannot_forge_p2_restore(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    with pytest.raises(InvalidOperation,match="safe-stop"):
        call(dsn,checkpoint_operation,lease,checkpoint_ref="opaque://untrusted")
    assert check(dsn,
        "SELECT checkpoint_ref FROM operations WHERE operation_id=%s",(op,),
    )==[(None,)]
    assert check(dsn,
        """SELECT count(*) FROM events WHERE event_type='operation.checkpointed.v1'
           AND payload->>'operation_id'=%s""",(str(op),),
    )==[(0,)]
    _return(dsn,user,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert call(dsn,reap_invalid_background_queue)>=1
    assert _row(dsn,op)[0]=="BLOCKED"


def test_expired_worker_with_past_task_deadline_is_not_retried(dsn,monkeypatch):
    user,instance,task,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE goal_tasks
               SET deadline=clock_timestamp()-interval '1 second'
               WHERE task_id=%s""",(task,),
        )
        conn.execute(
            """UPDATE operations
               SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert _row(dsn,op)[0]=="FAILED"
    assert _resources(dsn,op)==0
    with pytest.raises(StaleOperation):
        call(dsn,complete_operation,lease,result_ref="too-late")
    assert call(dsn,run_one_agent_step,worker_id="p405d-deadline",
                executor=lambda text,words:"should not run") is None
