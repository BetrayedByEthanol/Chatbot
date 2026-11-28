"""Cross-session acceptance through actual P0, Memory, Cognitive and output owners.

Fixed gateways validate contracts and context, not model quality/calibration.
Every call uses a new DB connection. Recovery explicitly abandons a real
claimed attempt after durable owner commit and uses the existing P0 reaper.
"""
import json
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.cognitive_contracts import CognitiveDraft,CognitiveProposal
from persona_agent.cognitive_engine import (
    schedule_cognition,prepare_cognition,commit_cognition,run_cognition,
    list_cognitive_proposals,list_eligible_cognitive_intentions,
)
from persona_agent.contextual_expectations import read_expectation
from persona_agent.prediction_lifecycle import read_prediction,inspect_prediction_history
from persona_agent.conversation import (
    prepare_text_context,commit_text_response,acknowledge_text_delivery,lookup_response,
)
from persona_agent.conversation_contracts import ModelReply,InvalidatedResponse
from persona_agent.experience_significance import list_experience_significance
from persona_agent.session_review import review_session_range,SessionReviewNotReady
from persona_agent.subjective_context import inspect_subjective_intentions
from persona_agent.operation_control import reap_stale_operations
from persona_agent.runtime import PermissionDenied
from test_phase2_cognition import actor,call,seed,platform,claim,cancel_pending
from test_p2f11_predictive_admission import set_enabled,reset
from test_p2f12_contextual_expectations import available_cognitive_slots,stage,reflect,report,EmptyModel
from test_p2f13_prediction_lifecycle import forecast,incoming
from test_p2f14_subjective_projection import prepare,reply
from test_p2f15_metamemory_significance import IMPORTANT
from test_mvp05d5_metamemory import _ask,_feedback
from workflow_evidence_helpers import link_memory,invalidate


def review(dsn,user,instance,conv,end,**kwargs):
    return call(dsn,review_session_range,actor(user),instance,conv,end_sequence=end,**kwargs)


def findings(view,kind):
    return [f for f in view.findings if f.kind==kind]


def close_and_claim(dsn,user,instance,conv):
    incoming(dsn,user,instance,"Continue discussing.",conv)
    scheduled=call(dsn,schedule_cognition,platform(user),conv,instance,1,
                   min_closed_turns=1,max_turns=16)
    assert scheduled is not None
    lease=claim(dsn)
    assert lease.operation_id==scheduled.operation_id
    return lease


class NoModel:
    def generate(self,messages):
        raise AssertionError("durable committed cognition must not infer again")


def test_cross_session_forecast_projection_recovery_intention_and_source_correction(dsn):
    user,instance,alternate=seed(dsn,other=True)
    foreign,_,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        # Earlier sessions establish a tentative hypothesis and a prospective
        # forecast; neither derives authority or fact confidence from model text.
        pid,prior,plan_conv,commitment=forecast(dsn,user,instance,False)
        assert call(dsn,read_prediction,actor(user),instance,pid).state=="PENDING"
        assert call(dsn,read_expectation,actor(user),alternate,"daily walk") is None
        assert call(dsn,inspect_prediction_history,actor(user),alternate)==()
        with pytest.raises(PermissionDenied):
            call(dsn,read_prediction,actor(foreign),instance,pid)

        # An independently received report is compared and delivered before
        # its asynchronous interpretation changes the prior expectation.
        outcome=incoming(dsn,user,instance,report(0))
        assert outcome.conversation_id!=plan_conv
        context=prepare(dsn,user,outcome)
        assert context.subjective_projection.kind=="MISMATCH"
        assert context.subjective_projection.expectation_revision_id==prior.revision_id
        body="I appreciate that update. This fixture is learned presentation, not evidence."
        generated=reply(dsn,user,context,body)
        call(dsn,acknowledge_text_delivery,actor(user),generated.response_id)
        important=incoming(dsn,user,instance,IMPORTANT,outcome.conversation_id)
        lease=close_and_claim(dsn,user,instance,outcome.conversation_id)
        work=call(dsn,prepare_cognition,lease)
        assert work.turns[0]["turn_id"]==outcome.turn_id
        assert work.turns[0]["response_text"]!=body
        assert work.turns[1]["experience_attention"]
        assert work.prediction_batch.resolutions[0].resolution=="MISS"
        draft=CognitiveDraft(proposals=(CognitiveProposal(
            kind="QUESTION",subject="CONVERSATION",content="What made the garden project meaningful?",
            source_turn_id=important.turn_id,evidence_quote=IMPORTANT,confidence=0.5,
        ),))
        committed=call(dsn,commit_cognition,lease,work,draft,"integrated-fixture")
        assert committed.reflection_count==1

        # Crash between owner commit and P0 completion. Advance only this
        # fixture's clock, then reclaim through P0; no second model pass.
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
                         (lease.operation_id,))
        call(dsn,reap_stale_operations,limit=1000)
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE operations SET eligible_at=clock_timestamp() WHERE operation_id=%s",
                         (lease.operation_id,))
        recovered=claim(dsn)
        assert recovered.operation_id==lease.operation_id
        assert recovered.claim_generation>lease.claim_generation
        replay=call(dsn,run_cognition,recovered,NoModel())
        assert replay.replayed and replay.reflection_count==1
        resolved=call(dsn,read_prediction,actor(user),instance,pid)
        assert resolved.resolution=="MISS" and resolved.outcome_turn_ids==(outcome.turn_id,)
        current=call(dsn,read_expectation,actor(user),instance,"daily walk")
        assert current.version==prior.version+1
        assert set(current.source_turn_ids)==set(prior.source_turn_ids)|{outcome.turn_id}
        assert commitment.turn_id not in current.source_turn_ids
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM cognitive_prediction_resolutions WHERE prediction_id=%s",
                                (pid,)).fetchone()==(1,)
            assert conn.execute("SELECT count(*) FROM cognitive_reflections WHERE operation_id=%s",
                                (lease.operation_id,)).fetchone()==(1,)
        # A previous response may have been delivered, yet its now-stale
        # interpretation cannot be offered as a fresh answer.
        with pytest.raises(InvalidatedResponse):
            call(dsn,lookup_response,actor(user),outcome.turn_id)
        view=review(dsn,user,instance,outcome.conversation_id,2)
        assert view.cognitive_complete and view.feedback=="NO_FEEDBACK"
        assert findings(view,"EXPERIENCE")[0].source_turn_id==important.turn_id
        assert findings(view,"REFLECTION_PROPOSAL")[0].state=="QUESTION"
        assert call(dsn,list_experience_significance,actor(user),alternate)==()

        # A later matching non-completion forms a conditional support
        # intention, consumed only on another independent relevant report.
        reflect(dsn,user,instance,report(2,False))
        intention,=call(dsn,inspect_subjective_intentions,actor(user),instance)
        assert intention["effective_status"]=="REQUIRES_CONDITION_AND_SOURCE_CHECK"
        unrelated=incoming(dsn,user,instance,"Explain soil drainage.")
        assert prepare(dsn,user,unrelated).subjective_projection is None
        later=incoming(dsn,user,instance,report(1,False))
        support=prepare(dsn,user,later)
        assert support.subjective_projection.kind=="SUPPORT"
        assert support.subjective_projection.intention_id==intention["intention_id"]
        response=reply(dsn,user,support)
        call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
        reserved,=call(dsn,inspect_subjective_intentions,actor(user),instance)
        assert reserved["effective_status"]=="RESERVED" and not reserved["already_asked"]

        # Exact Memory correction invalidates earlier predictive dependencies
        # without deleting the independent significance/question or ordinary
        # user reports. No negative mood/goal mutation follows a forecast miss.
        sid=prior.source_turn_ids[0]
        with psycopg.connect(dsn,autocommit=True) as conn:
            cid=conn.execute("SELECT conversation_id FROM conversation_turns WHERE turn_id=%s",(sid,)).fetchone()[0]
        saved=link_memory(dsn,user,instance,cid,sid)
        invalidate(dsn,user,instance,saved,"RETRACT")
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        assert call(dsn,read_expectation,actor(user),instance,"daily walk") is None
        with pytest.raises(InvalidatedResponse):
            call(dsn,lookup_response,actor(user),later.turn_id)
        fresh=incoming(dsn,user,instance,report(5,False))
        assert prepare(dsn,user,fresh).subjective_projection is None
        assert call(dsn,list_experience_significance,actor(user),instance)[0].source_turn_id==important.turn_id
        # The whole model-visible pass is pinned; the later question is
        # independent of the earlier prediction, which was host-only.
        assert any(p["kind"]=="QUESTION" for p in call(dsn,list_cognitive_proposals,actor(user),instance))
        set_enabled(dsn,user,instance,1,False)
        reset(dsn,user,instance,2)
        set_enabled(dsn,user,instance,3,True)
        assert call(dsn,read_prediction,actor(user),instance,pid) is None
        assert call(dsn,read_expectation,actor(user),instance,"daily walk") is None
        reflect(dsn,user,instance,report(5,False))
        # Report(5) received in enabled generation before reset was excluded;
        # only the genuinely new admission may seed a cold-start hypothesis.
        rebuilt=call(dsn,read_expectation,actor(user),instance,"daily walk")
        assert rebuilt.maturity=="EARLY" and len(rebuilt.source_turn_ids)==1
        assert call(dsn,list_experience_significance,actor(user),instance)[0].source_turn_id==important.turn_id
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM goal_records WHERE user_id=%s",(user,)).fetchone()==(0,)


def test_repeated_cross_session_interpretation_is_not_corroboration_and_stale_link_is_masked(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        from test_phase2_cognition import FixedCognition
        conv,first,lease=stage(dsn,user,instance,"Garden progress was slow.")
        saved=link_memory(dsn,user,instance,conv,first[0].turn_id)
        call(dsn,run_cognition,lease,FixedCognition(intention=False))
        old,=call(dsn,list_cognitive_proposals,actor(user),instance)
        _,second,lease=stage(dsn,user,instance,"Garden progress was discussed again.")
        call(dsn,run_cognition,lease,FixedCognition(intention=False))
        new,original=call(dsn,list_cognitive_proposals,actor(user),instance)
        assert new["related_reflection_id"]==old["reflection_id"]==original["reflection_id"]
        assert new["evidence_turn_id"]==second[0].turn_id
        assert new["confidence"]==old["confidence"]
        assert new["epistemic_status"]==old["epistemic_status"]=="MODEL_INTERPRETED"
        assert call(dsn,read_expectation,actor(user),instance,"daily walk") is None
        invalidate(dsn,user,instance,saved,"RETRACT")
        surviving,=call(dsn,list_cognitive_proposals,actor(user),instance)
        assert surviving["reflection_id"]==new["reflection_id"]
        assert surviving["related_reflection_id"] is None
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT related_reflection_id FROM cognitive_reflections WHERE reflection_id=%s",
                                (new["reflection_id"],)).fetchone()==(old["reflection_id"],)
            assert conn.execute("SELECT count(*) FROM memory_records WHERE user_id=%s AND record_status='ACTIVE'",
                                (user,)).fetchone()==(0,)


def test_session_review_neutral_ending_and_pending_range(dsn):
    user,instance,alternate=seed(dsn,other=True)
    foreign,_,_=seed(dsn)
    source=incoming(dsn,user,instance,"Thanks, that's all.")
    with pytest.raises(SessionReviewNotReady):
        review(dsn,user,instance,source.conversation_id,1)
    response=reply(dsn,user,prepare(dsn,user,source),"Goodbye.")
    with pytest.raises(SessionReviewNotReady):
        review(dsn,user,instance,source.conversation_id,1)
    call(dsn,acknowledge_text_delivery,actor(user),response.response_id)
    view=review(dsn,user,instance,source.conversation_id,1)
    assert view.feedback=="NO_FEEDBACK" and view.findings==()
    assert not view.cognitive_complete and not view.summary_complete
    assert view.eligible_turn_count==1 and not view.truncated
    for wrong_user,wrong_instance in ((user,alternate),(foreign,instance)):
        with pytest.raises(PermissionDenied):
            review(dsn,wrong_user,wrong_instance,source.conversation_id,1)
    with pytest.raises(ValueError):
        review(dsn,user,instance,source.conversation_id,65)


@pytest.mark.parametrize("outcome",["MISSED","INCORRECT"])
def test_session_review_reports_explicit_dissatisfaction_and_drops_stale_feedback(dsn,outcome):
    from persona_agent.metamemory import record_retrieval_feedback
    user,instance,_=seed(dsn)
    req,answer=_ask(dsn,user,instance,"What car do I drive?")
    cmd=_feedback(dsn,user,instance,req,answer,outcome=outcome)
    accepted=call(dsn,record_retrieval_feedback,actor(user),cmd)
    incoming(dsn,user,instance,"Thanks.",req.conversation_id)
    before=review(dsn,user,instance,req.conversation_id,2)
    assert before.feedback=="DISSATISFACTION_REPORTED"
    gap,=findings(before,"RECALL_GAP")
    assert gap.reference_id==answer.response_id and gap.state=="REVIEW_REQUIRED"
    feedback,=findings(before,"RETRIEVAL_FEEDBACK")
    assert feedback.reference_id==accepted.feedback_id and feedback.related_id==answer.response_id
    assert feedback.source_turn_id==cmd.feedback_turn_id and feedback.state==outcome
    # Owner read does not mutate or create adaptation policy.
    saved=link_memory(dsn,user,instance,req.conversation_id,cmd.feedback_turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    after=review(dsn,user,instance,req.conversation_id,2)
    assert after.feedback=="NO_FEEDBACK"
    assert not findings(after,"RETRIEVAL_FEEDBACK")
    assert findings(after,"RECALL_GAP")[0].state=="OPEN"
    assert after.eligible_turn_count==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_retrieval_feedback WHERE feedback_id=%s",
                            (accepted.feedback_id,)).fetchone()==(1,)


def test_session_review_uses_exact_approved_goal_and_report_not_inferred_success(dsn):
    from test_phase3_goals import root,authorize
    from persona_agent.goal_contracts import RecordGoalEvidenceCommand
    from persona_agent.goal_state import record_goal_evidence,list_goals
    user,instance,_=seed(dsn)
    source=incoming(dsn,user,instance,"I want to improve my eating habits.")
    gid,created,approval=root(dsn,user,instance,source.conversation_id,source)
    evidence=incoming(dsn,user,instance,"I prepared a balanced lunch.")
    accepted=call(dsn,record_goal_evidence,actor(user),RecordGoalEvidenceCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=evidence.conversation_id,evidence_turn_id=evidence.turn_id,
        evidence_quote="I prepared a balanced lunch.",goal_id=gid,expected_goal_version=1,
        evidence_kind="PROGRESS",evidence_id=uuid4(),
    ))
    incoming(dsn,user,instance,"Continue.",evidence.conversation_id)
    baseline=call(dsn,list_goals,actor(user),instance)
    view=review(dsn,user,instance,evidence.conversation_id,1)
    report_finding,=findings(view,"GOAL_REPORT")
    assert report_finding.reference_id==accepted.entity_id
    assert report_finding.related_id==gid and report_finding.state=="APPROVED_GOAL_REPORT"
    assert report_finding.report_kind=="PROGRESS" and report_finding.owner_version==2
    assert view.feedback=="NO_FEEDBACK"
    assert call(dsn,list_goals,actor(user),instance)==baseline
    saved=link_memory(dsn,user,instance,approval.conversation_id,approval.approval_turn_id)
    invalidate(dsn,user,instance,saved,"RETRACT")
    after=review(dsn,user,instance,evidence.conversation_id,1)
    assert findings(after,"GOAL_REPORT")[0].state=="GOAL_REVIEW_REQUIRED"
    assert call(dsn,list_goals,actor(user),instance)==baseline


def test_session_review_does_not_reopen_resolved_cross_session_gap(dsn):
    from persona_agent.memory import admit_memory
    from test_phase2_memory import candidate
    user,instance,_=seed(dsn)
    req,answer=_ask(dsn,user,instance,"What car do I drive?")
    assert findings(review(dsn,user,instance,req.conversation_id,1),"RECALL_GAP")[0].state=="OPEN"
    source=incoming(dsn,user,instance,"I drive a Toyota Camry.")
    call(dsn,admit_memory,actor(user),candidate(instance,source.conversation_id,source,
        kind="FACT",predicate="car",value="Toyota Camry",quote="I drive a Toyota Camry."))
    _,known=_ask(dsn,user,instance,"What car do I drive?")
    assert "Toyota Camry" in known.text
    assert not findings(review(dsn,user,instance,req.conversation_id,1),"RECALL_GAP")
