"""MVP-06D: authorized supplied-source research and diagnostic adaptation."""
from uuid import uuid4

import pytest

from persona_agent.goal_contracts import (
    ResearchGoalStrategiesCommand,ProposeResearchStrategyCommand,
    StrategySourcePin,goal_action_approval_phrase,
)


def helpers():
    pytest.importorskip("psycopg")
    from test_phase3_goals import actor,call,seed,turn,authorize
    return actor,call,seed,turn,authorize


def setup(dsn,*,user=None,instance=None,conv=None):
    from persona_agent.goal_contracts import CreateGoalCommand
    from persona_agent.goal_state import create_goal
    actor,call,seed,turn,authorize=helpers()
    if user is None:
        user,instance,_=seed(dsn)
        conv=uuid4()
    source=turn(dsn,user,instance,conv,"I want a working local TLS test endpoint.")
    cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="I want a working local TLS test endpoint.",
        goal_id=uuid4(),title="Local TLS",desired_outcome="Working local TLS test endpoint",
        success_criterion="Local client negotiates TLS with expected certificate",horizon="SHORT",
    )
    call(dsn,create_goal,actor(user),authorize(dsn,user,instance,cmd))
    return user,instance,conv,cmd.goal_id


def research_command(dsn,user,instance,conv,goal,query="certificate",version=1,*,approve=True):
    _,_,_,turn,authorize=helpers()
    source=turn(dsn,user,instance,conv,"Research saved sources for alternatives to my goal.")
    cmd=ResearchGoalStrategiesCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Research saved sources for alternatives to my goal.",
        goal_id=goal,expected_goal_version=version,query=query,
    )
    return authorize(dsn,user,instance,cmd) if approve else cmd


def candidate(dsn,user,instance,conv,request,pins,*,approve=True):
    _,_,_,turn,authorize=helpers()
    source=turn(dsn,user,instance,conv,"Consider this alternative based on the saved source.")
    cmd=ProposeResearchStrategyCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Consider this alternative based on the saved source.",
        goal_id=request.goal_id,expected_goal_version=request.expected_goal_version,
        strategy_id=uuid4(),title="Local certificate approach",
        approach="Use a dedicated local certificate and verify the handshake",
        suitability_note="Candidate for the local test only; verify applicability",
        limitations="Not evidence of public certificate trust or successful execution.",
        research=request,sources=tuple(pins),
    )
    return authorize(dsn,user,instance,cmd) if approve else cmd


def select_command(dsn,user,instance,conv,goal,strategy,version):
    from persona_agent.goal_contracts import SelectGoalStrategyCommand
    _,_,_,turn,authorize=helpers()
    source=turn(dsn,user,instance,conv,"I choose this particular strategy.")
    cmd=SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="I choose this particular strategy.",
        goal_id=goal,strategy_id=strategy,expected_goal_version=version,
    )
    return authorize(dsn,user,instance,cmd)


def test_source_research_requires_exact_approval_and_never_activates(dsn):
    import psycopg
    from test_mvp05d4_document_knowledge import _save_command,_withdraw_command
    from persona_agent.knowledge_memory import save_knowledge,withdraw_knowledge
    from persona_agent.goal_state import propose_goal_strategy,select_goal_strategy,list_goals,list_goal_strategies
    from persona_agent.strategy_research import research_goal_strategies,source_pin,compare_goal_strategies
    from persona_agent.runtime import PermissionDenied,VersionConflict
    actor,call,seed,_,_=helpers()
    user,instance,conv,goal=setup(dsn)
    source=_save_command(dsn,user,instance,
        content="For local certificate testing, use a dedicated certificate and verify the TLS handshake.")
    call(dsn,save_knowledge,actor(user),source)
    restricted=_save_command(dsn,user,instance,privacy="RESTRICTED",
        content="Confidential certificate notes must not be used.")
    call(dsn,save_knowledge,actor(user),restricted)
    foreign,other,_=seed(dsn)
    foreign_doc=_save_command(dsn,foreign,other,
        content="Foreign certificate source must not be disclosed.")
    call(dsn,save_knowledge,actor(foreign),foreign_doc)
    request=research_command(dsn,user,instance,conv,goal)
    with pytest.raises(PermissionDenied):
        call(dsn,research_goal_strategies,actor(user),request.model_copy(update={"approval_turn_id":None}))
    with pytest.raises(PermissionDenied):
        call(dsn,research_goal_strategies,actor(user),request.model_copy(update={"query":"handshake"}))
    with pytest.raises(PermissionDenied):
        call(dsn,research_goal_strategies,actor(foreign),request)
    result=call(dsn,research_goal_strategies,actor(user),request)
    assert result.status=="SOURCES_AVAILABLE" and not result.independently_verified
    hit,=result.sources
    assert hit.document_id==source.document_id and hit.passage_text==source.content
    assert hit.source_locator==source.source_locator and hit.epistemic_status=="SUPPLIED_SOURCE"
    empty=research_command(dsn,user,instance,conv,goal,"unrelated")
    assert call(dsn,research_goal_strategies,actor(user),empty).status=="NO_ELIGIBLE_SOURCE"
    assert call(dsn,list_goal_strategies,actor(user),instance,goal)==()
    pin=source_pin(hit)
    missing=candidate(dsn,user,instance,conv,request,[pin],approve=False)
    with pytest.raises(PermissionDenied):
        call(dsn,propose_goal_strategy,actor(user),missing)
    forged=pin.model_copy(update={"content_sha256":"0"*64})
    with pytest.raises(VersionConflict,match="research source unavailable"):
        call(dsn,propose_goal_strategy,actor(user),candidate(dsn,user,instance,conv,request,[forged]))
    cmd=candidate(dsn,user,instance,conv,request,[pin])
    created=call(dsn,propose_goal_strategy,actor(user),cmd)
    assert created.action=="STRATEGY_PROPOSED"
    assert call(dsn,propose_goal_strategy,actor(user),cmd).replayed
    root,=call(dsn,list_goals,actor(user),instance)
    assert root.selected_strategy_id is None and root.version==1
    comparison=call(dsn,compare_goal_strategies,actor(user),instance,goal)
    item,=comparison.strategies
    assert item.source_status=="CURRENT_SUPPLIED_SOURCE"
    assert not item.selected and not item.independently_verified_effectiveness
    assert item.limitations==cmd.limitations and item.sources==(hit,) and item.observations==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT document_id,version,position,content_sha256 FROM goal_strategy_research_sources
               WHERE strategy_id=%s""",(cmd.strategy_id,)
        ).fetchone()==(pin.document_id,pin.version,pin.position,pin.content_sha256)
        assert conn.execute("SELECT count(*) FROM operations WHERE instance_id=%s",(instance,)).fetchone()==(0,)
    choice=select_command(dsn,user,instance,conv,goal,cmd.strategy_id,1)
    call(dsn,withdraw_knowledge,actor(user),_withdraw_command(dsn,user,instance,source.document_id,1))
    with pytest.raises(VersionConflict,match="research source unavailable"):
        call(dsn,select_goal_strategy,actor(user),choice)
    with pytest.raises(VersionConflict,match="research source unavailable"):
        call(dsn,propose_goal_strategy,actor(user),candidate(dsn,user,instance,conv,request,[pin]))
    stale,=call(dsn,compare_goal_strategies,actor(user),instance,goal).strategies
    assert stale.source_status=="SOURCE_UNAVAILABLE" and stale.sources==()
    assert not stale.selected
    assert call(dsn,research_goal_strategies,actor(user),request).sources==()
    assert call(dsn,list_goals,actor(user),instance)[0].version==1


def test_researched_selection_is_separate_and_sources_version_fenced(dsn):
    from test_mvp05d4_document_knowledge import _save_command
    from persona_agent.knowledge_memory import save_knowledge
    from persona_agent.goal_state import propose_goal_strategy,select_goal_strategy,list_goals
    from persona_agent.strategy_research import research_goal_strategies,source_pin,compare_goal_strategies
    from persona_agent.runtime import PermissionDenied,VersionConflict
    actor,call,_,_,_=helpers()
    user,instance,conv,goal=setup(dsn)
    doc=_save_command(dsn,user,instance)
    call(dsn,save_knowledge,actor(user),doc)
    req=research_command(dsn,user,instance,conv,goal)
    pin=source_pin(call(dsn,research_goal_strategies,actor(user),req).sources[0])
    cmd=candidate(dsn,user,instance,conv,req,[pin])
    call(dsn,propose_goal_strategy,actor(user),cmd)
    selection=select_command(dsn,user,instance,conv,goal,cmd.strategy_id,1)
    with pytest.raises(PermissionDenied):
        call(dsn,select_goal_strategy,actor(user),selection.model_copy(update={"approval_turn_id":None}))
    selected=call(dsn,select_goal_strategy,actor(user),selection)
    assert selected.goal_version==2
    root,=call(dsn,list_goals,actor(user),instance)
    assert root.desired_outcome=="Working local TLS test endpoint"
    assert root.selected_strategy_id==cmd.strategy_id
    with pytest.raises(VersionConflict,match="research goal changed"):
        call(dsn,research_goal_strategies,actor(user),req)
    updated=_save_command(dsn,user,instance,document_id=doc.document_id,expected=1,
        content="Certificate configuration: a different current reference.")
    call(dsn,save_knowledge,actor(user),updated)
    row,=call(dsn,compare_goal_strategies,actor(user),instance,goal).strategies
    # Withdrawal/revision invalidates evidence, not an already authorized
    # historical selection. No automatic replan or private text export.
    assert row.selected and row.source_status=="SOURCE_UNAVAILABLE" and row.sources==()
    assert call(dsn,list_goals,actor(user),instance)[0].version==2


def test_strategy_comparison_and_approved_private_adaptation(dsn):
    import psycopg
    from test_phase3_runs import published,start_command,record
    from test_phase3_procedures import variant_command
    from test_phase3_learning import evaluation_command,proposal_command,decide_command
    from persona_agent.goal_contracts import ProposeGoalStrategyCommand
    from persona_agent.goal_state import propose_goal_strategy,select_goal_strategy,list_goals
    from persona_agent.procedure_state import create_personalized_approach,read_personalized_approach
    from persona_agent.procedure_run_state import start_procedure_run,read_procedure_run
    from persona_agent.procedure_learning_state import (
        read_adaptation_guidance,evaluate_procedure_run,propose_procedure_adaptation,decide_procedure_adaptation,
    )
    from persona_agent.strategy_research import compare_goal_strategies
    actor,call,_,turn,_=helpers()
    user,persona,instance,conv,template=published(dsn,"PARAMETERIZED")
    _,_,_,goal=setup(dsn,user=user,instance=instance,conv=conv)
    def alternative(title):
        source=turn(dsn,user,instance,conv,"Consider this local TLS approach.")
        cmd=ProposeGoalStrategyCommand(
            instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
            conversation_id=conv,evidence_turn_id=source.turn_id,
            evidence_quote="Consider this local TLS approach.",
            goal_id=goal,strategy_id=uuid4(),title=title,
            approach="Configure a local endpoint",suitability_note="Local testing only",
        )
        call(dsn,propose_goal_strategy,actor(user),cmd)
        return cmd.strategy_id
    first=alternative("Local certificate")
    second=alternative("Alternative local setup")
    call(dsn,select_goal_strategy,actor(user),select_command(dsn,user,instance,conv,goal,first,1))
    personal=variant_command(dsn,user,instance,conv,template,1,
        params={"hostname":"localhost","days":30},goal=goal,strategy=first)
    call(dsn,create_personalized_approach,actor(user),personal)
    start=start_command(dsn,user,instance,conv,template,variant=personal.variant_id,revision=1)
    call(dsn,start_procedure_run,actor(user),start)
    run=start.run_id
    assert call(dsn,read_adaptation_guidance,actor(user),instance,run).next_action=="ASSESS_OUTCOME"
    failure=record(dsn,user,instance,conv,run,1,"generate",1,"FAILED",
        detail="Local setup does not meet the intended method requirements.")
    assessed=evaluation_command(dsn,user,instance,conv,run,2,outcome="USER_REPORTED_FAILURE",
        cause="UNDETERMINED",step_events=(failure.step_event_id,))
    call(dsn,evaluate_procedure_run,actor(user),assessed)
    assert call(dsn,read_adaptation_guidance,actor(user),instance,run).next_action=="CLARIFY_CAUSE"
    revised=evaluation_command(dsn,user,instance,conv,run,2,outcome="USER_REPORTED_FAILURE",
        cause="METHOD_MISMATCH",prior=assessed.evaluation_id,step_events=(failure.step_event_id,))
    call(dsn,evaluate_procedure_run,actor(user),revised)
    guidance=call(dsn,read_adaptation_guidance,actor(user),instance,run)
    assert guidance.next_action=="CONSIDER_PARAMETER_CHANGE"
    assert guidance.evaluation_id==revised.evaluation_id and guidance.requires_approval
    compared=call(dsn,compare_goal_strategies,actor(user),instance,goal)
    a,b=compared.strategies
    assert a.strategy_id==first and a.selected and b.strategy_id==second and not b.selected
    observation,=a.observations
    assert observation.evaluation_id==revised.evaluation_id
    assert observation.outcome=="USER_REPORTED_FAILURE" and observation.variant_revision==1
    assert not a.independently_verified_effectiveness and b.observations==()
    proposal=proposal_command(dsn,user,instance,conv,revised.evaluation_id,personal.variant_id,1,
        params={"hostname":"localhost","days":60})
    call(dsn,propose_procedure_adaptation,actor(user),proposal)
    assert call(dsn,read_personalized_approach,actor(user),instance,personal.variant_id).revision==1
    decision=decide_command(dsn,user,instance,conv,proposal.proposal_id,1,"APPLY")
    from persona_agent.runtime import PermissionDenied
    with pytest.raises(PermissionDenied):
        call(dsn,decide_procedure_adaptation,actor(user),decision.model_copy(update={"approval_turn_id":None}))
    call(dsn,decide_procedure_adaptation,actor(user),decision)
    assert call(dsn,read_personalized_approach,actor(user),instance,personal.variant_id).revision==2
    assert call(dsn,read_procedure_run,actor(user),instance,run).variant_revision==1
    assert call(dsn,read_adaptation_guidance,actor(user),instance,run).next_action=="REASSESS_STALE_EVIDENCE"
    next_run=start_command(dsn,user,instance,conv,template,variant=personal.variant_id,revision=2)
    call(dsn,start_procedure_run,actor(user),next_run)
    observations=call(dsn,compare_goal_strategies,actor(user),instance,goal).strategies[0].observations
    assert {(o.variant_revision,o.outcome) for o in observations}=={(1,"USER_REPORTED_FAILURE"),(2,"UNEVALUATED")}
    # A new obstacle event invalidates the former run assessment.
    record(dsn,user,instance,conv,run,2,"generate",2,"FAILED",
        detail="The endpoint is still unavailable after a fresh check.")
    observations=call(dsn,compare_goal_strategies,actor(user),instance,goal).strategies[0].observations
    assert {(o.variant_revision,o.outcome) for o in observations}=={(1,"STALE"),(2,"UNEVALUATED")}
    root,=call(dsn,list_goals,actor(user),instance)
    assert root.version==2 and root.selected_strategy_id==first
    assert root.desired_outcome=="Working local TLS test endpoint"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM operations WHERE instance_id=%s",(instance,)).fetchone()==(0,)
        assert conn.execute("SELECT count(*) FROM procedure_template_versions WHERE template_id=%s",(template,)).fetchone()==(1,)


@pytest.mark.parametrize("policy,outcome,cause,expected",[
    ("STABLE","USER_REPORTED_FAILURE","METHOD_MISMATCH","DIAGNOSE_STABLE_METHOD"),
    ("ADAPTIVE","USER_REPORTED_FAILURE","ENVIRONMENT","CHECK_ENVIRONMENT"),
    ("PARAMETERIZED","USER_REPORTED_FAILURE","INCORRECT_INPUT","CHECK_INPUTS"),
    ("PARAMETERIZED","USER_REPORTED_FAILURE","INVALID_SUCCESS_CRITERIA","REVIEW_SUCCESS_CRITERIA"),
    ("ADAPTIVE","INCOMPLETE",None,"COMPLETE_REQUIRED_ACTION"),
])
def test_failure_guidance_is_diagnostic_and_nonmutating(dsn,policy,outcome,cause,expected):
    from test_phase3_learning import prepared_variant_run,evaluation_command
    from persona_agent.procedure_learning_state import evaluate_procedure_run,read_adaptation_guidance
    from persona_agent.procedure_state import read_personalized_approach
    from persona_agent.procedure_run_state import read_procedure_run
    actor,call,_,_,_=helpers()
    user,instance,conv,template,variant,run=prepared_variant_run(dsn,policy)
    assessment=evaluation_command(dsn,user,instance,conv,run,1,outcome=outcome,cause=cause)
    call(dsn,evaluate_procedure_run,actor(user),assessment)
    view=call(dsn,read_adaptation_guidance,actor(user),instance,run)
    assert view.next_action==expected and not view.independently_verified
    assert call(dsn,read_personalized_approach,actor(user),instance,variant).revision==1
    assert call(dsn,read_procedure_run,actor(user),instance,run).version==1
