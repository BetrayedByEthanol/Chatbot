"""P4-05b: real-PostgreSQL safe-stop checkpoint and data-only restoration."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.operation_control import (
    CancelOperationCommand,acknowledge_suspend,cancel_operation,
    reap_stale_operations,request_background_suspension,
)
from persona_agent.operation_contracts import StaleOperation,InvalidOperation
from persona_agent.procedure_agent_checkpoint import (
    load_validated_procedure_checkpoint,
)
from persona_agent.procedure_agent_state import run_one_agent_step
from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.runtime import PermissionDenied,StaleSelection,VersionConflict
from test_phase4_preemption import _admitted,_return,_lease
from test_phase4_preparation import call,check,user_actor


@pytest.fixture(autouse=True)
def retire_other_queued_agent_steps(dsn):
    """No test may accidentally consume a different user's P2 claim."""
    from persona_agent.tool_contracts import PROCEDURE_AGENT_OPERATION
    with psycopg.connect(dsn,autocommit=True) as conn:
        pending=conn.execute(
            """SELECT operation_id,user_id,version FROM operations
               WHERE operation_type=%s AND status IN ('QUEUED','RETRY_WAIT')""",
            (PROCEDURE_AGENT_OPERATION,),
        ).fetchall()
    for op,user,version in pending:
        call(dsn,cancel_operation,user_actor(user),CancelOperationCommand(
            operation_id=op,expected_version=version,idempotency_key=uuid4().hex,
        ))


def _checkpointed(dsn,monkeypatch):
    user,instance,task,run,op=_admitted(dsn,monkeypatch)

    def stop_inside_read_only_tool(instruction,max_words):
        _return(dsn,user,instance)
        return "discarded result; not a checkpoint"

    lease=call(dsn,run_one_agent_step,worker_id=f"ckpt-{uuid4().hex[:10]}",
               executor=stop_inside_read_only_tool)
    assert lease is not None and lease.operation_id==op
    return user,instance,task,run,op,lease


def test_cooperative_safe_stop_commits_immutable_checkpoint_and_restores_input(dsn,monkeypatch):
    user,instance,task,run,op,lease=_checkpointed(dsn,monkeypatch)
    rows=check(dsn,
        """SELECT o.status,o.effect_status,o.checkpoint_ref,
                  c.boundary,c.schema_version,c.claim_generation,
                  c.background_epoch,c.max_words
           FROM operations o JOIN procedure_agent_checkpoints c
             ON c.operation_id=o.operation_id
           WHERE o.operation_id=%s""",(op,),
    )
    assert rows==[(
        "SUSPENDED","PREPARED",f"procedure-step:v1:{op}:{lease.claim_generation}",
        "READ_ONLY_RESTART_REQUIRED",1,lease.claim_generation,1,5,
    )]
    restored=call(dsn,load_validated_procedure_checkpoint,user_actor(user),
                  operation_id=op)
    assert restored.operation_id==op
    assert restored.run_id==run and restored.task_id==task
    assert restored.step_id=="inspect" and restored.max_words==5
    assert restored.source_background_epoch==1 and restored.restart_required
    assert isinstance(restored.instruction,str) and restored.instruction
    # Reading data is not renewed permission or a tool/Task transition.
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT status FROM procedure_run_steps WHERE run_id=%s AND step_id='inspect'",
        (run,),
    )==[("NOT_STARTED",)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results WHERE operation_id=%s",
        (op,),
    )==[(0,)]
    assert check(dsn,
        """SELECT count(*) FROM operation_resource_reservations
           WHERE operation_id=%s""",(op,),
    )==[(0,)]
    assert check(dsn,
        """SELECT count(*) FROM events
           WHERE event_type='operation.checkpointed.v1'
             AND payload->>'operation_id'=%s""",(str(op),),
    )==[(1,)]
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                """UPDATE procedure_agent_checkpoints SET max_words=6
                   WHERE operation_id=%s""",(op,),
            )


def test_p0_expired_worker_recovery_never_fabricates_checkpoint(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    assert lease is not None and lease.operation_id==op
    _return(dsn,user,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    assert call(dsn,reap_stale_operations)>=1
    assert check(dsn,
        "SELECT status,checkpoint_ref FROM operations WHERE operation_id=%s",(op,),
    )==[("SUSPENDED",None)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_checkpoints WHERE operation_id=%s",(op,),
    )==[(0,)]
    with pytest.raises(VersionConflict):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=op)


def test_checkpoint_restoration_rejects_changed_task_owner_versions(dsn,monkeypatch):
    user,_,task,_,op,_=_checkpointed(dsn,monkeypatch)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE goal_tasks SET version=version+1 WHERE task_id=%s",(task,))
    with pytest.raises(VersionConflict):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=op)


def test_checkpoint_restoration_rejects_changed_instruction_digest(dsn,monkeypatch):
    user,_,_,run,op,_=_checkpointed(dsn,monkeypatch)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE procedure_run_steps SET instruction=instruction||' changed'
               WHERE run_id=%s AND step_id='inspect'""",(run,),
        )
    with pytest.raises(VersionConflict,match="instruction differs"):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=op)


def test_checkpoint_restoration_rejects_cross_user_and_selection_change(dsn,monkeypatch):
    user,_,_,_,op,_=_checkpointed(dsn,monkeypatch)
    with pytest.raises(PermissionDenied):
        call(dsn,load_validated_procedure_checkpoint,user_actor(uuid4()),
             operation_id=op)
    # A persona switch revokes selection generation. Simulate the post-switch
    # generation here without introducing a separate test persona.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE persona_selection SET generation=generation+1 WHERE user_id=%s",
            (user,),
        )
    with pytest.raises(StaleSelection):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=op)


def test_checkpoint_pointer_mismatch_fails_closed(dsn,monkeypatch):
    user,_,_,_,op,_=_checkpointed(dsn,monkeypatch)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE operations SET checkpoint_ref='unsafe:source' WHERE operation_id=%s",
            (op,),
        )
    with pytest.raises(VersionConflict,match="no valid immutable checkpoint"):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=op)


def test_modified_step_at_safe_stop_still_releases_resources_no_checkpoint(dsn,monkeypatch):
    user,_,_,run,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    assert lease is not None and lease.operation_id==op
    _return(dsn,user,check(dsn,
        "SELECT instance_id FROM operations WHERE operation_id=%s",(op,),
    )[0][0])
    # Input changed after the executor stopped. No safe evidence can
    # represent this Procedure version, but ACK must not strand resources.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE procedure_run_steps SET version=version+1 "
            "WHERE run_id=%s AND step_id='inspect'",(run,),
        )
    preemptor=TrustedPlatformActor(user_id=user,source_id="background.preemption")
    assert call(dsn,request_background_suspension,preemptor,op)
    call(dsn,acknowledge_suspend,lease)
    assert check(dsn,
        "SELECT status,checkpoint_ref FROM operations WHERE operation_id=%s",(op,),
    )==[("SUSPENDED",None)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
        (op,),
    )==[(0,)]


def test_stale_acknowledgement_cannot_write_checkpoint(dsn,monkeypatch):
    user,instance,_,_,op=_admitted(dsn,monkeypatch)
    lease=_lease(dsn)
    _return(dsn,user,instance)
    preemptor=TrustedPlatformActor(user_id=user,source_id="background.preemption")
    assert call(dsn,request_background_suspension,preemptor,op)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations SET lease_until=clock_timestamp()-interval '1 second'
               WHERE operation_id=%s""",(op,),
        )
    with pytest.raises(StaleOperation):
        call(dsn,acknowledge_suspend,lease)
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_checkpoints WHERE operation_id=%s",(op,),
    )==[(0,)]
    assert call(dsn,reap_stale_operations)>=1
