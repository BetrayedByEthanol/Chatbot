"""MVP-03A.4: real PostgreSQL P2 READY epoch and Goal-owner admissions."""
import json
from uuid import uuid4
import pytest
psycopg=pytest.importorskip("psycopg")

import test_phase3_goals as goals
from test_phase4_autonomy import ready
from test_phase4_preparation import (
    call,FastResponse,user_actor,configure_resource_pool,
)
from persona_agent.conversation import (
    respond_to_text,acknowledge_text_delivery,
)
from persona_agent.conversation_contracts import ModelReply
from persona_agent.goal_autonomy import (
    schedule_goal_evaluation,run_goal_evaluation_scan,
    run_one_goal_evaluation,
)
from persona_agent.goal_autonomy_contracts import (
    GOAL_EVALUATION_OPERATION,GOAL_EVALUATION_CAPABILITY,
)
from persona_agent.goal_decomposition import propose_derived_subgoal
from persona_agent.goal_decomposition_contracts import ProposeDerivedSubgoalCommand
from persona_agent.lifecycle import TrustedPlatformActor
from persona_agent.operation_contracts import (
    EffectClass,EnqueueOperation,Origin,Policy,Priority,
)
from persona_agent.operation_scheduler import (
    enqueue_operation,claim_operation,
)
from persona_agent.runtime import PermissionDenied,StaleSelection


def stage_actor(user):
    return TrustedPlatformActor(user_id=user,source_id="goal.autonomy.scheduler")


def closed_goal(dsn,monkeypatch):
    # Actually deliver the approved user's goal turn, so the P4 preparation
    # barrier can process a truthful source rather than bypassing watermarks.
    old=goals.turn
    def close(*args,**kwargs):
        source=old(*args,**kwargs)
        result=call(
            dsn,respond_to_text,user_actor(args[1]),source.turn_id,
            FastResponse(),
        )
        call(dsn,acknowledge_text_delivery,user_actor(args[1]),
             result.response_id)
        return source
    monkeypatch.setattr(goals,"turn",close)
    call(dsn,configure_resource_pool,"cognition.llm",
         capacity_units=3,p0_reserved_units=1)
    user,instance,_,root=goals.approved_book_goal(dsn)
    return user,instance,root


class OpportunityModel:
    def __init__(self,candidate=None,callback=None):
        self.candidate=candidate
        self.callback=callback
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        assert len(messages)==2
        assert messages[0].role=="system"
        data=json.loads(messages[1].content)
        assert data["approved_goal"]["desired_outcome"]=="Read 3 books per month"
        if self.callback:
            self.callback()
        return ModelReply(model_id="goal-opportunity-fake",
                          text=json.dumps({"candidate":self.candidate}))


def outcome(*,title="Read regularly",desired="Read 1 book per month",
            criterion="Read at least 1 book per month"):
    return dict(
        title=title,desired_outcome=desired,
        success_criterion=criterion,horizon="SHORT",
        contribution_note="Lower count supports the approved monthly target",
    )


def test_goal_autonomy_requires_ready_background_epoch(dsn,monkeypatch):
    user,instance,root=closed_goal(dsn,monkeypatch)
    with pytest.raises(StaleSelection):
        call(dsn,schedule_goal_evaluation,
             stage_actor(user),instance,1)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_autonomy_jobs WHERE user_id=%s",
            (user,),
        ).fetchone()[0]==0


def test_ready_epoch_stages_and_executes_one_p0_goal_operation(dsn,monkeypatch):
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    staged=call(dsn,schedule_goal_evaluation,
                stage_actor(user),instance,1)
    assert staged.goal_id==root
    again=call(dsn,schedule_goal_evaluation,
               stage_actor(user),instance,1)
    assert again.operation_id==staged.operation_id and again.replayed
    with psycopg.connect(dsn,autocommit=True) as conn:
        op=conn.execute(
            """SELECT operation_type,origin,priority_class,execution_policy,
                      effect_class,task_id,background_epoch
               FROM operations WHERE operation_id=%s""",
            (staged.operation_id,),
        ).fetchone()
        assert op==(GOAL_EVALUATION_OPERATION,"GOAL","P2",
                    "BACKGROUND_RESUMABLE","NONE",None,1)
    model=OpportunityModel(outcome())
    assert call(dsn,run_one_goal_evaluation,model,
                worker_id="mvp03a4")== "PROPOSED"
    assert model.calls==1
    created=call(dsn,goals.list_goals,goals.actor(user),instance)
    assert len(created)==2
    child=next(g for g in created if g.goal_id!=root)
    assert child.creation_authority=="DELEGATED_VERIFIED"
    assert child.parent_goal_id==root
    assert child.success_criterion=="Read at least 1 book per month"
    assert next(g for g in created if g.goal_id==root).desired_outcome=="Read 3 books per month"
    assert call(dsn,run_one_goal_evaluation,model,worker_id="mvp03a4") is None
    assert call(dsn,schedule_goal_evaluation,
                stage_actor(user),instance,1) is None
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT status,proposal_id,proposed_goal_id,model_id
               FROM goal_autonomy_jobs WHERE operation_id=%s""",
            (staged.operation_id,),
        ).fetchone()
        assert row[0]=="PROPOSED"
        assert row[1] is not None and row[2]==child.goal_id
        assert row[3]=="goal-opportunity-fake"
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="COMPLETED"


def test_unprovable_model_candidate_is_only_pending_review(dsn,monkeypatch):
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    assert len(call(dsn,run_goal_evaluation_scan,limit=32))>=1
    model=OpportunityModel(outcome(
        title="Daily reading",
        desired="Read 15 pages per day",
        criterion="Read at least 15 pages per day",
    ))
    assert call(dsn,run_one_goal_evaluation,model,worker_id="mvp03a4-pending")=="PROPOSED"
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==1
    pending=call(dsn,goals.list_goals,goals.actor(user),instance)
    assert pending[0].goal_id==root
    with psycopg.connect(dsn,autocommit=True) as conn:
        proposal=conn.execute(
            """SELECT p.status,p.decision_method FROM goal_subgoal_proposals p
               JOIN goal_autonomy_jobs j ON j.proposal_id=p.proposal_id
               WHERE j.user_id=%s AND j.instance_id=%s""",
            (user,instance),
        ).fetchone()
        assert proposal==("PENDING_REVIEW","REVIEW_REQUIRED")


def test_no_opportunity_marks_job_complete_without_subgoal(dsn,monkeypatch):
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    call(dsn,schedule_goal_evaluation,stage_actor(user),instance,1)
    model=OpportunityModel(None)
    assert call(dsn,run_one_goal_evaluation,model,worker_id="mvp03a4-none")=="NO_PROPOSAL"
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_autonomy_jobs WHERE goal_id=%s",
            (root,),
        ).fetchone()[0]=="NO_PROPOSAL"


def test_unstaged_goal_p2_cannot_be_enqueued(dsn,monkeypatch):
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    from persona_agent.goal_autonomy_contracts import GOAL_EVALUATION_RESOURCE
    command=EnqueueOperation(
        operation_id=uuid4(),instance_id=instance,expected_generation=1,
        idempotency_key="forged-goal-stage",
        operation_type=GOAL_EVALUATION_OPERATION,origin=Origin.GOAL,
        execution_policy=Policy.BACKGROUND_RESUMABLE,
        priority_class=Priority.P2,effect_class=EffectClass.NONE,
        resources={GOAL_EVALUATION_RESOURCE:1},
        capabilities=(GOAL_EVALUATION_CAPABILITY,),
        concurrency_group=f"goal.evaluation:{instance}",
    )
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,stage_actor(user),command)
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==1


def test_foreground_return_during_goal_model_aborts_unapproved_write(dsn,monkeypatch):
    from persona_agent.lifecycle import (
        record_primary_activity,PrimaryActivityCommand,
    )
    from persona_agent.operation_contracts import StaleOperation
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    call(dsn,schedule_goal_evaluation,stage_actor(user),instance,1)
    def user_return():
        call(dsn,record_primary_activity,user_actor(user),
             PrimaryActivityCommand(
                 instance_id=instance,expected_generation=1,
                 idempotency_key=str(uuid4()),
             ))
    model=OpportunityModel(outcome(),callback=user_return)
    with pytest.raises((StaleOperation,StaleSelection,PermissionDenied)):
        call(dsn,run_one_goal_evaluation,model,worker_id="mvp03a4-return")
    assert model.calls==1
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_subgoal_proposals WHERE parent_goal_id=%s",
            (root,),
        ).fetchone()[0]==0


def test_capacity_pressure_does_not_abort_other_ready_epochs(dsn,monkeypatch):
    import persona_agent.goal_autonomy as autonomy
    from persona_agent.operation_contracts import ResourceUnavailable
    first_user,first_instance,_=closed_goal(dsn,monkeypatch)
    ready(dsn,first_user,first_instance)
    monkeypatch.undo()
    second_user,second_instance,_=closed_goal(dsn,monkeypatch)
    ready(dsn,second_user,second_instance)
    original=autonomy.schedule_goal_evaluation
    visited=[]
    def admission(connection,actor,instance_id,epoch):
        visited.append(actor.user_id)
        if actor.user_id==first_user:
            raise ResourceUnavailable("P2 capacity currently full")
        return original(connection,actor,instance_id,epoch)
    monkeypatch.setattr(autonomy,"schedule_goal_evaluation",admission)
    scheduled=call(dsn,autonomy.run_goal_evaluation_scan,limit=24)
    assert first_user in visited and second_user in visited
    assert len(scheduled)==1 and scheduled[0].goal_id is not None
    # The suite shares one PostgreSQL database. Retire both READY jobs so
    # a later run_one_goal_evaluation cannot claim another test's operation.
    monkeypatch.setattr(autonomy,"schedule_goal_evaluation",original)
    first=call(dsn,autonomy.schedule_goal_evaluation,
               stage_actor(first_user),first_instance,1)
    assert first is not None
    assert call(dsn,run_one_goal_evaluation,OpportunityModel(None),
                worker_id="capacity-cleanup-1")=="NO_PROPOSAL"
    assert call(dsn,run_one_goal_evaluation,OpportunityModel(None),
                worker_id="capacity-cleanup-2")=="NO_PROPOSAL"


def test_model_transport_failure_retries_and_releases_resources(dsn,monkeypatch):
    from persona_agent.conversation_contracts import ModelUnavailable
    from persona_agent.goal_autonomy_worker import run_goal_evaluation_tick
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    class UnavailableModel:
        def generate(self,messages):
            raise ModelUnavailable("local provider unavailable")
    staged,executed=call(dsn,run_goal_evaluation_tick,UnavailableModel())
    assert len(staged)==1 and executed is None
    op_id=staged[0].operation_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        op=conn.execute(
            "SELECT status,attempt_count FROM operations WHERE operation_id=%s",
            (op_id,),
        ).fetchone()
        assert op==("RETRY_WAIT",1)
        assert conn.execute(
            "SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
            (op_id,),
        ).fetchone()[0]==0
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
            (op_id,),
        )
    model=OpportunityModel(outcome())
    assert call(dsn,run_goal_evaluation_tick,model,worker_id="retry-worker")[1]=="PROPOSED"
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==2


def test_candidate_finalization_rolls_back_atomically(dsn,monkeypatch):
    import persona_agent.goal_autonomy as autonomy
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    staged=call(dsn,schedule_goal_evaluation,stage_actor(user),instance,1)
    original=autonomy._finalize_goal_evaluation_locked
    def crash_before_finalization(*args,**kwargs):
        raise RuntimeError("simulated crash after Goal mutation")
    monkeypatch.setattr(autonomy,"_finalize_goal_evaluation_locked",crash_before_finalization)
    with pytest.raises(RuntimeError):
        call(dsn,run_one_goal_evaluation,OpportunityModel(outcome()),
             worker_id="rollback-worker")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_subgoal_proposals WHERE parent_goal_id=%s",
            (root,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT status FROM goal_autonomy_jobs WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="STAGED"
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="RETRY_WAIT"
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
            (staged.operation_id,),
        )
    monkeypatch.setattr(autonomy,"_finalize_goal_evaluation_locked",original)
    assert call(dsn,run_one_goal_evaluation,OpportunityModel(outcome()),
                worker_id="after-rollback")=="PROPOSED"
    assert len(call(dsn,goals.list_goals,goals.actor(user),instance))==2


def test_no_proposal_result_is_atomic_with_p0_completion(dsn,monkeypatch):
    import persona_agent.goal_autonomy as autonomy
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    staged=call(dsn,schedule_goal_evaluation,stage_actor(user),instance,1)
    original=autonomy.event_for
    def fail_event(*args,**kwargs):
        raise RuntimeError("simulated completion interruption")
    monkeypatch.setattr(autonomy,"event_for",fail_event)
    with pytest.raises(RuntimeError):
        call(dsn,run_one_goal_evaluation,OpportunityModel(None),
             worker_id="none-rollback")
    monkeypatch.setattr(autonomy,"event_for",original)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_autonomy_jobs WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="STAGED"
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="RETRY_WAIT"
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
            (staged.operation_id,),
        )
    assert call(dsn,run_one_goal_evaluation,OpportunityModel(None),
                worker_id="none-cleanup")=="NO_PROPOSAL"


def test_expired_lease_after_ancestry_lock_cannot_commit(dsn,monkeypatch):
    import persona_agent.goal_decomposition as decomposition
    user,instance,root=closed_goal(dsn,monkeypatch)
    ready(dsn,user,instance)
    staged=call(dsn,schedule_goal_evaluation,stage_actor(user),instance,1)
    original=decomposition._locked_active_goal_ancestry
    def expire_after_lock(cur,actor,instance_id,parent_id):
        parent=original(cur,actor,instance_id,parent_id)
        cur.execute(
            "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
            (staged.operation_id,),
        )
        return parent
    monkeypatch.setattr(decomposition,"_locked_active_goal_ancestry",expire_after_lock)
    from persona_agent.operation_contracts import StaleOperation
    with pytest.raises(StaleOperation):
        call(dsn,run_one_goal_evaluation,OpportunityModel(outcome()),
             worker_id="expired-lock")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_subgoal_proposals WHERE parent_goal_id=%s",
            (root,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT status FROM goal_autonomy_jobs WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="STAGED"
    # Do not strand the retry in the shared integration database.
    monkeypatch.setattr(decomposition,"_locked_active_goal_ancestry",original)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (staged.operation_id,),
        ).fetchone()[0]=="RETRY_WAIT"
        conn.execute(
            "UPDATE operations SET eligible_at=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
            (staged.operation_id,),
        )
    assert call(dsn,run_one_goal_evaluation,OpportunityModel(None),
                worker_id="expiry-cleanup")=="NO_PROPOSAL"
