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
