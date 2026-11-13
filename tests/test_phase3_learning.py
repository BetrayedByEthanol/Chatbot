"""P3-05 PostgreSQL acceptance: attributable method assessment and safe revision."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.procedure_learning_contracts import (
    DecideProcedureAdaptationCommand, EvaluateProcedureRunCommand,
    ProcedureCriterionReport, ProposeProcedureAdaptationCommand,
)
from persona_agent.procedure_learning_state import (
    decide_procedure_adaptation, evaluate_procedure_run,
    propose_procedure_adaptation, read_procedure_adaptation,
    read_procedure_evaluations,
)
from persona_agent.procedure_state import (
    create_personalized_approach, read_personalized_approach,
)
from persona_agent.procedure_run_state import (
    read_procedure_run, start_procedure_run,
)
from persona_agent.runtime import PermissionDenied, VersionConflict
from persona_agent.task_state import list_tasks
from test_phase3_procedures import (
    actor, approve, call, seed, turn, variant_command,
)
from test_phase3_runs import published, record, run_status, start_command
from test_phase3_tasks import make_goal, make_task


def evaluation_command(dsn,user,instance,conv,run,version,*,outcome,
                       cause=None,prior=None,step_events=(),criteria=()):
    message=turn(dsn,user,instance,conv,
                 "I observed this outcome when trying the guided method.")
    return EvaluateProcedureRunCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=message.turn_id,
        evidence_quote="I observed this outcome",
        evaluation_id=uuid4(),run_id=run,expected_run_version=version,
        expected_previous_evaluation_id=prior,
        outcome=outcome,failure_cause=cause,confidence=0.7,
        rationale="Reported experience with the selected method in this environment",
        supporting_step_event_ids=step_events,
        criteria=tuple(
            ProcedureCriterionReport(position=pos,state=state)
            for pos,state in criteria
        ),
    )


def proposal_command(dsn,user,instance,conv,evaluation_id,variant,revision,*,
                     params=None,overrides=None):
    message=turn(dsn,user,instance,conv,
                 "Consider adjusting my personal procedure, without changing goals.")
    return ProposeProcedureAdaptationCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=message.turn_id,
        evidence_quote="Consider adjusting my personal procedure",
        proposal_id=uuid4(),evaluation_id=evaluation_id,
        variant_id=variant,expected_revision=revision,
        parameters=params or {},step_text_overrides=overrides or {},
        reason="Retry using documented personal settings after checking the cause",
    )


def decide_command(dsn,user,instance,conv,proposal_id,revision,decision):
    message=turn(dsn,user,instance,conv,
                 "I choose the recorded personal procedure recommendation.")
    cmd=DecideProcedureAdaptationCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=message.turn_id,
        evidence_quote="I choose the recorded personal procedure recommendation",
        proposal_id=proposal_id,expected_revision=revision,decision=decision,
    )
    return approve(dsn,user,instance,cmd)


def prepared_variant_run(dsn,policy="ADAPTIVE"):
    user,persona,instance,conv,template=published(dsn,policy)
    params={"hostname":"localhost","days":30} if policy!="STABLE" else {}
    personal=variant_command(
        dsn,user,instance,conv,template,1,params=params,
    )
    call(dsn,create_personalized_approach,actor(user),personal)
    started=start_command(
        dsn,user,instance,conv,template,
        variant=personal.variant_id,revision=1,
    )
    call(dsn,start_procedure_run,actor(user),started)
    return user,instance,conv,template,personal.variant_id,started.run_id


def test_evaluation_tracks_factual_step_evidence_without_task_or_goal_completion(dsn):
    user,persona,instance,conv,template=published(dsn)
    goal=make_goal(dsn,user,instance,conv)
    task,_,_=make_task(dsn,user,instance,conv,goal,1)
    started=start_command(dsn,user,instance,conv,template,task=task)
    call(dsn,start_procedure_run,actor(user),started)
    attempt=record(dsn,user,instance,conv,started.run_id,1,
                   "generate",1,"FAILED",
                   detail="The input hostname was wrong and validation failed")
    assessment=evaluation_command(
        dsn,user,instance,conv,started.run_id,2,
        outcome="USER_REPORTED_FAILURE",cause="INCORRECT_INPUT",
        step_events=(attempt.step_event_id,),
        criteria=((1,"USER_REPORTED_UNMET"),),
    )
    outcome=call(dsn,evaluate_procedure_run,actor(user),assessment)
    assert outcome.action=="RUN_EVALUATED" and outcome.revision==1
    assert call(dsn,evaluate_procedure_run,actor(user),assessment).replayed
    read=call(dsn,read_procedure_evaluations,actor(user),instance,started.run_id)
    assert len(read)==1
    assert read[0].failure_cause=="INCORRECT_INPUT"
    assert read[0].supporting_step_event_ids==(attempt.step_event_id,)
    assert read[0].criteria[0].state=="USER_REPORTED_UNMET"
    assert not read[0].independently_verified
    assert not read[0].task_or_goal_completed
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status=="PLANNED"
    assert call(dsn,read_procedure_run,actor(user),instance,
                started.run_id).steps[0].status=="FAILED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_evidence WHERE goal_id=%s",(goal,),
        ).fetchone()[0]==0
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "DELETE FROM procedure_run_evaluations WHERE evaluation_id=%s",
                (assessment.evaluation_id,),
            )


def test_reported_success_requires_finished_run_and_all_pinned_criteria(dsn):
    user,persona,instance,conv,template=published(dsn)
    started=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),started)
    premature=evaluation_command(
        dsn,user,instance,conv,started.run_id,1,
        outcome="USER_REPORTED_SUCCESS",criteria=((1,"USER_REPORTED_MET"),),
    )
    with pytest.raises(VersionConflict,match="finished"):
        call(dsn,evaluate_procedure_run,actor(user),premature)
    first=record(dsn,user,instance,conv,started.run_id,1,
                 "generate",1,"USER_REPORTED_DONE")
    confirmed=record(dsn,user,instance,conv,started.run_id,2,
                     "generate",2,"USER_CONFIRMED")
    report=record(dsn,user,instance,conv,started.run_id,3,
                  "check",1,"USER_REPORTED_DONE")
    verified=record(dsn,user,instance,conv,started.run_id,4,
                    "check",2,"USER_CONFIRMED")
    finished=run_status(dsn,user,instance,conv,started.run_id,5,"FINISH")
    assert finished.version==6
    missing=evaluation_command(
        dsn,user,instance,conv,started.run_id,6,
        outcome="USER_REPORTED_SUCCESS",
        step_events=(confirmed.step_event_id,verified.step_event_id),
    )
    with pytest.raises(VersionConflict,match="every criterion"):
        call(dsn,evaluate_procedure_run,actor(user),missing)
    success=evaluation_command(
        dsn,user,instance,conv,started.run_id,6,
        outcome="USER_REPORTED_SUCCESS",
        step_events=(confirmed.step_event_id,verified.step_event_id),
        criteria=((1,"USER_REPORTED_MET"),),
    )
    accepted=call(dsn,evaluate_procedure_run,actor(user),success)
    assert accepted.revision==1
    assert call(dsn,read_procedure_evaluations,actor(user),instance,
                started.run_id)[0].outcome=="USER_REPORTED_SUCCESS"
    assert call(dsn,read_procedure_run,actor(user),instance,
                started.run_id).status=="USER_CONFIRMED"


def test_parameterized_failure_proposal_requires_approval_and_never_rewrites_run(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"PARAMETERIZED")
    failed=record(dsn,user,instance,conv,run,1,"generate",1,"FAILED",
                  detail="The initial hostname did not resolve in the test environment")
    assessed=evaluation_command(
        dsn,user,instance,conv,run,2,
        outcome="USER_REPORTED_FAILURE",cause="ENVIRONMENT",
        step_events=(failed.step_event_id,),
    )
    call(dsn,evaluate_procedure_run,actor(user),assessed)
    bad=proposal_command(
        dsn,user,instance,conv,assessed.evaluation_id,variant,1,
        params={"hostname":"localhost","days":60},
        overrides={"generate":"Unsafe unsolicited instruction rewrite"},
    )
    with pytest.raises(VersionConflict,match="parameterized"):
        call(dsn,propose_procedure_adaptation,actor(user),bad)
    proposed=proposal_command(
        dsn,user,instance,conv,assessed.evaluation_id,variant,1,
        params={"hostname":"localhost","days":60},
    )
    result=call(dsn,propose_procedure_adaptation,actor(user),proposed)
    assert result.action=="ADAPTATION_PROPOSED"
    assert call(dsn,propose_procedure_adaptation,actor(user),proposed).replayed
    view=call(dsn,read_procedure_adaptation,actor(user),instance,proposed.proposal_id)
    assert view.status=="PROPOSED" and view.parameters["days"]==60
    assert call(dsn,read_personalized_approach,actor(user),instance,
                variant).revision==1
    before=call(dsn,read_procedure_run,actor(user),instance,run)
    cmd=decide_command(dsn,user,instance,conv,proposed.proposal_id,1,"APPLY")
    with pytest.raises(PermissionDenied,match="approval"):
        call(dsn,decide_procedure_adaptation,actor(user),
             cmd.model_copy(update={"approval_turn_id":None,
                                    "idempotency_key":uuid4().hex}))
    applied=call(dsn,decide_procedure_adaptation,actor(user),cmd)
    assert applied.action=="ADAPTATION_APPLIED" and applied.revision==2
    assert call(dsn,decide_procedure_adaptation,actor(user),cmd).replayed
    current=call(dsn,read_personalized_approach,actor(user),instance,variant)
    assert current.revision==2 and current.parameters["days"]==60
    historical=call(dsn,read_personalized_approach,actor(user),instance,variant,1)
    assert historical.parameters["days"]==30
    after=call(dsn,read_procedure_run,actor(user),instance,run)
    assert before.steps==after.steps and before.version==after.version
    assert after.variant_revision==1 and after.status=="ACTIVE"
    assert call(dsn,read_procedure_adaptation,actor(user),instance,
                proposed.proposal_id).status=="APPLIED"
    with pytest.raises(VersionConflict,match="already decided"):
        call(dsn,decide_procedure_adaptation,actor(user),decide_command(
            dsn,user,instance,conv,proposed.proposal_id,2,"REJECT",
        ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "UPDATE procedure_adaptation_proposals SET reason='tampered' "
                "WHERE proposal_id=%s",(proposed.proposal_id,),
            )


def test_adaptive_user_instruction_proposal_is_bounded_and_rejectable(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"ADAPTIVE")
    reported=record(dsn,user,instance,conv,run,1,"generate",1,"BLOCKED",
                    detail="The local certificate path was inaccessible")
    assessed=evaluation_command(
        dsn,user,instance,conv,run,2,
        outcome="USER_REPORTED_FAILURE",cause="METHOD_MISMATCH",
        step_events=(reported.step_event_id,),
    )
    call(dsn,evaluate_procedure_run,actor(user),assessed)
    params={"hostname":"localhost","days":30}
    proposed=proposal_command(
        dsn,user,instance,conv,assessed.evaluation_id,variant,1,
        params=params,
        overrides={"generate":"Use the approved alternate local folder for {hostname}"},
    )
    call(dsn,propose_procedure_adaptation,actor(user),proposed)
    rejected=call(dsn,decide_procedure_adaptation,actor(user),decide_command(
        dsn,user,instance,conv,proposed.proposal_id,1,"REJECT",
    ))
    assert rejected.action=="ADAPTATION_REJECTED"
    assert call(dsn,read_procedure_adaptation,actor(user),instance,
                proposed.proposal_id).status=="REJECTED"
    assert call(dsn,read_personalized_approach,actor(user),instance,variant
                ).revision==1
    with pytest.raises(VersionConflict,match="already decided"):
        call(dsn,decide_procedure_adaptation,actor(user),decide_command(
            dsn,user,instance,conv,proposed.proposal_id,1,"APPLY",
        ))


def test_stable_policy_and_success_feedback_cannot_propose_unauthorized_tuning(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"STABLE")
    issue=record(dsn,user,instance,conv,run,1,"generate",1,"FAILED",
                 detail="A system permission denied certificate creation")
    assessment=evaluation_command(
        dsn,user,instance,conv,run,2,
        outcome="USER_REPORTED_FAILURE",cause="ENVIRONMENT",
        step_events=(issue.step_event_id,),
    )
    call(dsn,evaluate_procedure_run,actor(user),assessment)
    new=proposal_command(
        dsn,user,instance,conv,assessment.evaluation_id,variant,1,
        overrides={"generate":"Rewrite my fixed certificate setup"},
    )
    with pytest.raises(VersionConflict,match="stable"):
        call(dsn,propose_procedure_adaptation,actor(user),new)


def test_supersession_prevents_stale_proposals_and_accepted_revision(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"PARAMETERIZED")
    first=evaluation_command(
        dsn,user,instance,conv,run,1,
        outcome="USER_REPORTED_FAILURE",cause="INCORRECT_INPUT",
    )
    call(dsn,evaluate_procedure_run,actor(user),first)
    proposal=proposal_command(
        dsn,user,instance,conv,first.evaluation_id,variant,1,
        params={"hostname":"localhost","days":60},
    )
    call(dsn,propose_procedure_adaptation,actor(user),proposal)
    updated=evaluation_command(
        dsn,user,instance,conv,run,1,
        outcome="UNKNOWN",prior=first.evaluation_id,
    )
    new=call(dsn,evaluate_procedure_run,actor(user),updated)
    assert new.revision==2
    history=call(dsn,read_procedure_evaluations,actor(user),instance,run)
    assert [r.evaluation_revision for r in history]==[2,1]
    assert history[0].supersedes_evaluation_id==first.evaluation_id
    with pytest.raises(VersionConflict,match="superseded"):
        call(dsn,propose_procedure_adaptation,actor(user),proposal_command(
            dsn,user,instance,conv,first.evaluation_id,variant,1,
            params={"hostname":"localhost","days":90},
        ))
    with pytest.raises(VersionConflict,match="evidence changed"):
        call(dsn,decide_procedure_adaptation,actor(user),decide_command(
            dsn,user,instance,conv,proposal.proposal_id,1,"APPLY",
        ))
    assert call(dsn,read_personalized_approach,actor(user),instance,variant
                ).revision==1


def test_concurrent_evaluation_requires_exact_previous_revision(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"PARAMETERIZED")
    commands=[
        evaluation_command(dsn,user,instance,conv,run,1,
                           outcome="UNKNOWN"),
        evaluation_command(dsn,user,instance,conv,run,1,
                           outcome="INCOMPLETE"),
    ]
    gate=Barrier(2)
    def evaluate_race(cmd):
        gate.wait(timeout=15)
        try:
            return call(dsn,evaluate_procedure_run,actor(user),cmd)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(evaluate_race,commands[0])
        b=pool.submit(evaluate_race,commands[1])
        outcomes=[a.result(timeout=20),b.result(timeout=20)]
    assert sum(item is not None for item in outcomes)==1
    assert len(call(dsn,read_procedure_evaluations,actor(user),instance,run))==1


def test_cross_persona_evidence_and_proposal_isolation(dsn):
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,"PARAMETERIZED")
    reported=record(dsn,user,instance,conv,run,1,"generate",1,"FAILED",
                    detail="The result failed in the local test environment")
    assessed=evaluation_command(
        dsn,user,instance,conv,run,2,
        outcome="USER_REPORTED_FAILURE",cause="ENVIRONMENT",
        step_events=(reported.step_event_id,),
    )
    call(dsn,evaluate_procedure_run,actor(user),assessed)
    outsider,_,foreign_instance=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,read_procedure_evaluations,actor(outsider),
             foreign_instance,run)
    with pytest.raises(PermissionDenied):
        call(dsn,propose_procedure_adaptation,actor(outsider),
             proposal_command(
                 dsn,outsider,foreign_instance,uuid4(),
                 assessed.evaluation_id,variant,1,
                 params={"hostname":"localhost","days":100},
             ))
