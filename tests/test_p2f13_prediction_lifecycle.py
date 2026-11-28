"""Real PostgreSQL, actual ingress, foreground read and leased Cognitive owner."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.cognitive_contracts import CognitiveDraft
from persona_agent.cognitive_engine import prepare_cognition,commit_cognition,run_cognition
from persona_agent.contextual_expectations import read_expectation,withdraw_expectation
from persona_agent.conversation import ingest_text,prepare_text_context
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.expectation_contracts import WithdrawExpectation
from persona_agent.prediction_contracts import WithdrawPrediction
from persona_agent.prediction_lifecycle import (
    compare_incoming_report,read_prediction,inspect_prediction_history,
    read_recorded_comparison,withdraw_prediction,
)
from persona_agent.runtime import PermissionDenied
from test_phase2_cognition import actor,call,seed,platform,claim,cancel_pending
from test_p2f11_predictive_admission import set_enabled,reset
from test_p2f12_contextual_expectations import (
    available_cognitive_slots,report,stage,reflect,EmptyModel,
)
from workflow_evidence_helpers import link_memory,invalidate


def plan(day=0,activity="daily walk"):
    occasion=datetime.now(timezone.utc).date()+timedelta(days=day)
    return f"On {occasion.isoformat()}, I plan to complete my {activity}."


def incoming(dsn,user,instance,text,conv=None):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        instance_id=instance,conversation_id=conv or uuid4(),expected_generation=1,
        idempotency_key=uuid4().hex,text=text))


def initialize(dsn,user,instance,completed=True):
    set_enabled(dsn,user,instance,0,True)
    reflect(dsn,user,instance,report(4,completed),report(3,completed))
    e=call(dsn,read_expectation,actor(user),instance,"daily walk")
    assert e.maturity=="TENTATIVE"
    return e


def forecast(dsn,user,instance,completed=True):
    expectation=initialize(dsn,user,instance,completed)
    conv,turns=reflect(dsn,user,instance,plan())
    history=call(dsn,inspect_prediction_history,actor(user),instance)
    assert len(history)==1
    pid=history[0]["prediction_id"]
    result=call(dsn,read_prediction,actor(user),instance,pid)
    assert result.state=="PENDING"
    assert result.expectation_revision_id==expectation.revision_id
    assert result.expected_completion==completed
    return pid,expectation,conv,turns[0]


def finish_input(dsn,user,instance,source):
    from persona_agent.cognitive_engine import schedule_cognition
    incoming(dsn,user,instance,"Continue discussing.",source.conversation_id)
    scheduled=call(dsn,schedule_cognition,platform(user),source.conversation_id,
                   instance,1,min_closed_turns=1,max_turns=16)
    assert scheduled is not None
    lease=claim(dsn)
    assert lease.operation_id==scheduled.operation_id
    call(dsn,run_cognition,lease,EmptyModel())


def resolution_count(dsn,pid):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute("SELECT count(*) FROM cognitive_prediction_resolutions WHERE prediction_id=%s",
                            (pid,)).fetchone()[0]


def comparison_ids(dsn,user,instance):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return [r[0] for r in conn.execute("""SELECT comparison_id FROM cognitive_expectation_comparisons
                     WHERE user_id=%s AND instance_id=%s ORDER BY created_at,comparison_id""",
                    (user,instance)).fetchall()]


@pytest.mark.parametrize("text,route",[
    ("Hello.","SKIP"),("Explain soil drainage.","SKIP"),
    ("I did it!","DEFER"),("I couldn't finish it.","DEFER"),
    ("Actually, that was yesterday.","DEFER"),
])
def test_irrelevant_and_ambiguous_routes_have_no_predictive_query(text,route):
    class NoQuery:
        def execute(self,*args,**kwargs):
            raise AssertionError("irrelevant or ambiguous route queried predictive state")
    read=compare_incoming_report(NoQuery(),uuid4(),uuid4(),uuid4(),text,
                                 datetime.now(timezone.utc))
    assert read.route==route and read.classification is None and read.fence is None


def test_cold_start_exact_report_unknown_and_no_prompt_projection(dsn):
    user,instance,_=seed(dsn)
    source=incoming(dsn,user,instance,report(0))
    context=call(dsn,prepare_text_context,actor(user),source.turn_id)
    assert context.prediction_comparison.route=="COMPARE_NOW"
    assert context.prediction_comparison.classification=="UNKNOWN"
    assert context.predictive_fence is None
    ordinary=incoming(dsn,user,instance,"Hello.")
    ordinary_context=call(dsn,prepare_text_context,actor(user),ordinary.turn_id)
    assert ordinary_context.prediction_comparison is None


@pytest.mark.parametrize("expected,observed",[(True,True),(True,False),(False,True),(False,False)])
def test_real_forecast_later_report_resolves_once_against_prior_revision(dsn,expected,observed):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        pid,e,_,commitment=forecast(dsn,user,instance,expected)
        # Exact pre-outcome source pin exists before any outcome is received.
        with psycopg.connect(dsn,autocommit=True) as conn:
            row=conn.execute("""SELECT source_turn_ids,expectation_version FROM cognitive_predictions
                                WHERE prediction_id=%s""",(pid,)).fetchone()
            assert set(row[0])==set(e.source_turn_ids)|{commitment.turn_id}
            assert row[1]==e.version
        outcome=incoming(dsn,user,instance,report(0,observed))
        context=call(dsn,prepare_text_context,actor(user),outcome.turn_id)
        label="EXPECTED" if expected==observed else "UNEXPECTED"
        comparison=context.prediction_comparison
        assert comparison.classification==label
        assert comparison.expectation_revision_id==e.revision_id
        assert set(comparison.fence.source_turn_ids)==set(e.source_turn_ids)|{outcome.turn_id}
        # P2-F1.4 now admits a real, bounded presentation hint for a
        # supported tentative mismatch, not for an ordinary match.
        if expected==observed:
            assert context.predictive_fence is None and context.subjective_projection is None
        else:
            assert context.subjective_projection.kind=="MISMATCH"
            assert context.predictive_fence==context.subjective_projection.fence
            assert context.subjective_projection.expectation_revision_id==e.revision_id
        assert all("COMPARABLE_USER_REPORT" not in m.content for m in context.messages)
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("""SELECT prediction_id,comparison_expectation_id
                                   FROM predictive_input_admissions WHERE turn_id=%s""",
                                (outcome.turn_id,)).fetchone()==(pid,e.revision_id)
        finish_input(dsn,user,instance,outcome)
        resolved=call(dsn,read_prediction,actor(user),instance,pid)
        assert resolved.state=="HISTORICAL"
        assert resolved.resolution==("HIT" if expected==observed else "MISS")
        assert resolved.comparison==label and resolved.outcome_turn_ids==(outcome.turn_id,)
        assert resolved.evidence_class=="USER_REPORTED"
        current=call(dsn,read_expectation,actor(user),instance,"daily walk")
        assert current.version==e.version+1
        assert current.revision_id!=resolved.expectation_revision_id
        cid,=comparison_ids(dsn,user,instance)
        recorded=call(dsn,read_recorded_comparison,actor(user),instance,cid)
        assert recorded.classification==label and recorded.historical
        duplicate=incoming(dsn,user,instance,report(0,observed))
        again=call(dsn,prepare_text_context,actor(user),duplicate.turn_id)
        assert again.prediction_comparison.classification=="UNKNOWN"
        finish_input(dsn,user,instance,duplicate)
        assert resolution_count(dsn,pid)==1
        assert comparison_ids(dsn,user,instance)==[cid]
        assert call(dsn,read_prediction,actor(user),instance,pid)==resolved
    other,_,_=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,read_prediction,actor(other),instance,pid)


def test_comparison_without_forecast_and_conflicting_same_occasion(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        e=initialize(dsn,user,instance)
        outcome=incoming(dsn,user,instance,report(2,False))
        read=call(dsn,prepare_text_context,actor(user),outcome.turn_id).prediction_comparison
        assert read.classification=="UNEXPECTED" and read.expectation_revision_id==e.revision_id
        finish_input(dsn,user,instance,outcome)
        previous,=comparison_ids(dsn,user,instance)
        contradictory=incoming(dsn,user,instance,report(2,True))
        read=call(dsn,prepare_text_context,actor(user),contradictory.turn_id).prediction_comparison
        assert read.classification=="CONTRADICTORY"
        assert outcome.turn_id in read.fence.source_turn_ids
        finish_input(dsn,user,instance,contradictory)
        assert call(dsn,read_recorded_comparison,actor(user),instance,previous) is None
        assert len(comparison_ids(dsn,user,instance))==2
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()


def test_conflicting_bound_reports_resolve_inconclusively(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        pid,_,_,_=forecast(dsn,user,instance)
        _,sources=reflect(dsn,user,instance,report(0),report(0,False))
        r=call(dsn,read_prediction,actor(user),instance,pid)
        assert r.resolution=="INCONCLUSIVE" and r.comparison=="CONTRADICTORY"
        assert set(r.outcome_turn_ids)=={x.turn_id for x in sources}
        assert resolution_count(dsn,pid)==1


def test_later_opposite_report_suppresses_resolution_without_second_resolution(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        pid,_,_,_=forecast(dsn,user,instance)
        reflect(dsn,user,instance,report(0))
        assert call(dsn,read_prediction,actor(user),instance,pid).resolution=="HIT"
        conflict=incoming(dsn,user,instance,report(0,False))
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        finish_input(dsn,user,instance,conflict)
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        assert resolution_count(dsn,pid)==1


def test_outcome_received_during_forecast_preparation_cannot_be_backdated(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance)
        _,_,lease=stage(dsn,user,instance,plan())
        work=call(dsn,prepare_cognition,lease)
        assert len(work.prediction_batch.forecasts)==1
        early=incoming(dsn,user,instance,report(0))
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT prediction_id FROM predictive_input_admissions WHERE turn_id=%s",
                                (early.turn_id,)).fetchone()==(None,)
        call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,lease,EmptyModel())
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()
        finish_input(dsn,user,instance,early)
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()


@pytest.mark.parametrize("action",["DISABLE","RESET","DISABLE_ENABLE","WITHDRAW_EXPECTATION"])
def test_forecast_preparation_rejects_late_control_or_revision_change(dsn,action):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        e=initialize(dsn,user,instance)
        _,_,lease=stage(dsn,user,instance,plan())
        work=call(dsn,prepare_cognition,lease)
        assert work.prediction_batch.forecasts
        if action=="DISABLE":
            set_enabled(dsn,user,instance,1,False)
        elif action=="RESET":
            reset(dsn,user,instance,1)
        elif action=="DISABLE_ENABLE":
            set_enabled(dsn,user,instance,1,False)
            set_enabled(dsn,user,instance,2,True)
        else:
            call(dsn,withdraw_expectation,actor(user),WithdrawExpectation(
                instance_id=instance,revision_id=e.revision_id,expected_version=e.version,
                expected_generation=1,idempotency_key="withdraw-expectation"))
        call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,lease,EmptyModel())
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()


@pytest.mark.parametrize("action",["DISABLE","RESET","WITHDRAW","RETRACT_SOURCE"])
def test_prepared_resolution_rejects_late_control_withdrawal_or_evidence_change(dsn,action):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        pid,_,conv,commitment=forecast(dsn,user,instance)
        if action=="RETRACT_SOURCE":
            saved=link_memory(dsn,user,instance,conv,commitment.turn_id)
        _,_,lease=stage(dsn,user,instance,report(0))
        work=call(dsn,prepare_cognition,lease)
        assert len(work.prediction_batch.resolutions)==1
        if action=="DISABLE":
            set_enabled(dsn,user,instance,1,False)
        elif action=="RESET":
            reset(dsn,user,instance,1)
        elif action=="WITHDRAW":
            command=WithdrawPrediction(instance_id=instance,prediction_id=pid,
                        expected_generation=1,idempotency_key="withdraw-prediction")
            result=call(dsn,withdraw_prediction,actor(user),command)
            assert call(dsn,withdraw_prediction,actor(user),command).replayed
            assert result.prediction_id==pid
        else:
            invalidate(dsn,user,instance,saved,"RETRACT")
        call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,lease,EmptyModel())
        assert resolution_count(dsn,pid)==0
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        if action=="DISABLE":
            set_enabled(dsn,user,instance,2,True)
            assert call(dsn,read_prediction,actor(user),instance,pid) is None


def test_resolved_outcome_retraction_suppresses_history_without_undoing_ordinary_evidence(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        pid,_,_,_=forecast(dsn,user,instance)
        conv,sources=reflect(dsn,user,instance,report(0))
        saved=link_memory(dsn,user,instance,conv,sources[0].turn_id)
        assert call(dsn,read_prediction,actor(user),instance,pid).resolution=="HIT"
        cid,=comparison_ids(dsn,user,instance)
        invalidate(dsn,user,instance,saved,"EXPIRE")
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        assert call(dsn,read_recorded_comparison,actor(user),instance,cid) is None
        assert resolution_count(dsn,pid)==1


def test_time_only_expiry_without_outcome_is_inconclusive(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        e=initialize(dsn,user,instance)
        _,_,lease=stage(dsn,user,instance,plan())
        work=call(dsn,prepare_cognition,lease)
        p,=work.prediction_batch.forecasts
        # Construct a historical clock-expired forecast fixture using real
        # scoped source pins and a short elapsed horizon. No UPDATE of
        # immutable records, wall-clock sleeps or fake outcome evidence.
        pid=uuid4()
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("""INSERT INTO cognitive_predictions(
                   prediction_id,user_id,instance_id,reset_generation,control_version,
                   expectation_revision_id,expectation_version,operation_id,commitment_turn_id,
                   context_key,target_occasion,expected_completion,source_turn_ids,created_at,expires_at)
                 SELECT %s,%s,%s,%s,%s,%s,%s,%s,t.turn_id,%s,%s,%s,%s,
                        t.created_at+interval '1 microsecond',clock_timestamp()-interval '1 microsecond'
                 FROM conversation_turns t WHERE t.turn_id=%s""",
                (pid,user,instance,p.fence.reset_generation,p.fence.control_version,
                 e.revision_id,e.version,lease.operation_id,p.context_key,p.occasion,
                 p.expected_completion,list(p.fence.source_turn_ids),p.commitment_turn_id))
        expired=call(dsn,read_prediction,actor(user),instance,pid)
        assert expired.state=="EXPIRED" and expired.resolution=="INCONCLUSIVE"
        assert expired.comparison=="UNKNOWN" and expired.outcome_turn_ids==()
        assert resolution_count(dsn,pid)==0
        # The real job's prospective insert cannot overwrite the historical
        # target; expired target never acquires an outcome retrospectively.
        call(dsn,run_cognition,lease,EmptyModel())
        assert call(dsn,read_prediction,actor(user),instance,pid)==expired



@pytest.mark.parametrize("history",["EARLY","MIXED","UNRELATED"])
def test_forecast_requires_sufficient_comparable_prior_history(dsn,history):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        set_enabled(dsn,user,instance,0,True)
        if history=="EARLY":
            reflect(dsn,user,instance,report(4))
        elif history=="MIXED":
            reflect(dsn,user,instance,report(4),report(3,False))
        else:
            reflect(dsn,user,instance,report(4,activity="reading practice"),
                    report(3,activity="reading practice"))
        reflect(dsn,user,instance,plan())
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()


def test_expired_claim_cannot_commit_prediction_or_comparison(dsn):
    from persona_agent.operation_contracts import StaleOperation
    from persona_agent.operation_control import reap_stale_operations
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance)
        _,_,lease=stage(dsn,user,instance,plan())
        work=call(dsn,prepare_cognition,lease)
        assert work.prediction_batch.forecasts
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
                         (lease.operation_id,))
        with pytest.raises(StaleOperation):
            call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        assert call(dsn,inspect_prediction_history,actor(user),instance)==()
        assert comparison_ids(dsn,user,instance)==[]
        call(dsn,reap_stale_operations,limit=1000)
        cancel_pending(dsn,user,lease.operation_id)


def test_competing_outcome_ranges_resolve_once_and_keep_independent_evidence(dsn):
    from persona_agent.expectation_contracts import ExpectationVersionConflict
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,2):
        pid,_,_,_=forecast(dsn,user,instance)
        _,first_sources,first=stage(dsn,user,instance,report(0))
        _,second_sources,second=stage(dsn,user,instance,report(0,False))
        one=call(dsn,prepare_cognition,first)
        two=call(dsn,prepare_cognition,second)
        assert one.prediction_batch.resolutions[0].resolution=="INCONCLUSIVE"
        assert two.prediction_batch.resolutions[0].resolution=="INCONCLUSIVE"
        assert one.expectation_plans[0].expected_version==two.expectation_plans[0].expected_version
        call(dsn,commit_cognition,first,one,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,first,EmptyModel())
        with pytest.raises(ExpectationVersionConflict):
            call(dsn,commit_cognition,second,two,CognitiveDraft(),"fixture")
        fresh=call(dsn,prepare_cognition,second)
        assert not fresh.prediction_batch.resolutions
        call(dsn,commit_cognition,second,fresh,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,second,EmptyModel())
        r=call(dsn,read_prediction,actor(user),instance,pid)
        assert r.resolution=="INCONCLUSIVE"
        assert set(r.outcome_turn_ids)=={first_sources[0].turn_id,second_sources[0].turn_id}
        assert resolution_count(dsn,pid)==1
        e=call(dsn,read_expectation,actor(user),instance,"daily walk")
        assert e.maturity=="CONFLICTED" and e.conflicting_occasions==1
