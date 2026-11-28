"""P2-F1.5: actual recall, source eligibility, compression and cognitive attention."""
import json
from datetime import datetime,timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.conversation import ingest_text,prepare_text_context
from persona_agent.conversation_contracts import TextInputCommand,ModelReply
from persona_agent.experience_significance import (
    explicit_experience_marker,list_experience_significance,
)
from persona_agent.metamemory import list_recall_observations,record_retrieval_feedback
from persona_agent.cognitive_engine import prepare_cognition,run_cognition,commit_cognition
from persona_agent.cognitive_contracts import CognitiveDraft,StaleCognition
from persona_agent.summary_memory import schedule_summary,prepare_summary,run_summary
from persona_agent.runtime import PermissionDenied
from test_phase2_cognition import actor,call,seed
from test_phase2_summaries import claim as claim_summary
from test_p2f12_contextual_expectations import available_cognitive_slots,stage
from test_p2f13_prediction_lifecycle import incoming
from test_mvp05d5_metamemory import _ask,_feedback
from workflow_evidence_helpers import link_memory,invalidate

IMPORTANT="This experience is important to me: I finished my garden project."


def markers(dsn,user,instance,**kwargs):
    return call(dsn,list_experience_significance,actor(user),instance,**kwargs)


def gaps(dsn,user,instance,**kwargs):
    return call(dsn,list_recall_observations,actor(user),instance,**kwargs)


@pytest.mark.parametrize("text,marked",[
    (IMPORTANT,True),
    ("  THIS EXPERIENCE IS IMPORTANT TO ME: I finished my garden project.  ",True),
    ("I finished my garden project.",False),
    ('"This experience is important to me: I finished my garden project."',False),
    ("Is this experience important to me?",False),
    ("This experience is important to me: ",False),
    ("This experience is important to me: my password changed.",False),
    ("This experience is important to me: "+("x"*601),False),
])
def test_significance_is_explicit_bounded_and_not_model_inferred(text,marked):
    assert explicit_experience_marker(text)==marked


def test_marker_is_scoped_idempotent_and_independent_of_learning_control(dsn):
    user,instance,other=seed(dsn,other=True)
    foreign,_,_=seed(dsn)
    conv=uuid4()
    cmd=TextInputCommand(instance_id=instance,conversation_id=conv,expected_generation=1,
                         idempotency_key="important",text=IMPORTANT)
    source=call(dsn,ingest_text,actor(user),cmd)
    assert call(dsn,ingest_text,actor(user),cmd).replayed
    incoming(dsn,user,instance,"I finished my garden project.")
    incoming(dsn,user,instance,"This experience is important to me: my password changed.")
    marker,=markers(dsn,user,instance)
    assert marker.source_turn_id==source.turn_id and marker.source_event_id==source.event_id
    assert marker.evidence_class=="USER_REPORTED" and marker.attention=="ACTIVE"
    assert (marker.attention_until-marker.marked_at).total_seconds()==168*3600
    assert markers(dsn,user,other)==()
    with pytest.raises(PermissionDenied):
        markers(dsn,foreign,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM predictive_input_admissions WHERE user_id=%s",(user,)).fetchone()==(0,)
        assert conn.execute("SELECT count(*) FROM memory_records WHERE user_id=%s",(user,)).fetchone()==(0,)


def test_significance_survives_summary_omission_and_informs_existing_cognition(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        conv,sources,lease=stage(dsn,user,instance,IMPORTANT,"I watered the garden.")
        class CognitiveGateway:
            def generate(self,messages):
                rows=json.loads(messages[-1].content)
                assert [r["turn_id"] for r in rows if r.get("experience_attention")=="USER_MARKED_IMPORTANT"]==[str(sources[0].turn_id)]
                assert "experience_attention" not in rows[1]
                return ModelReply(model_id="significance-fixture",text='{"proposals":[]}')
        call(dsn,run_cognition,lease,CognitiveGateway())
        job=call(dsn,schedule_summary,actor(user),conv,instance,1,retain_recent=1,max_turns=2)
        assert job is not None
        lease=claim_summary(dsn)
        assert lease.operation_id==job.operation_id
        work=call(dsn,prepare_summary,lease)
        assert work.turns[0]["experience_significant"] and not work.turns[1]["experience_significant"]
        class SummaryGateway:
            def generate(self,messages):
                rows=json.loads(messages[-1].content)
                assert rows[0]["experience_significance"]=="USER_MARKED_IMPORTANT"
                assert "experience_significance" not in rows[1]
                return ModelReply(model_id="significance-fixture",
                    text='{"summary":"The user discussed gardening.","episodes":[]}')
        done=call(dsn,run_summary,lease,SummaryGateway())
        assert done.episode_count==0
        marker,=markers(dsn,user,instance)
        assert marker.source_turn_id==sources[0].turn_id
        next_turn=incoming(dsn,user,instance,"Explain soil drainage.")
        context=call(dsn,prepare_text_context,actor(user),next_turn.turn_id)
        assert "USER_MARKED_IMPORTANT" not in context.messages[0].content
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT summary_watermark FROM conversations WHERE conversation_id=%s",(conv,)).fetchone()==(2,)
            assert conn.execute("SELECT count(*) FROM memory_records WHERE user_id=%s",(user,)).fetchone()==(0,)


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_marker_eligibility_follows_exact_source_without_erasure(dsn,change):
    user,instance,_=seed(dsn)
    source=incoming(dsn,user,instance,IMPORTANT)
    saved=link_memory(dsn,user,instance,source.conversation_id,source.turn_id)
    assert markers(dsn,user,instance)[0].source_turn_id==source.turn_id
    invalidate(dsn,user,instance,saved,change)
    assert markers(dsn,user,instance)==()
    assert markers(dsn,user,instance,active_only=True)==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_experience_markers WHERE source_turn_id=%s",(source.turn_id,)).fetchone()==(1,)


def test_aged_attention_is_not_deletion_or_source_invalidation(dsn,monkeypatch):
    import persona_agent.experience_significance as significance
    user,instance,_=seed(dsn)
    # Construct only the elapsed-clock marker fixture from a genuine
    # authenticated source; never UPDATE immutable metadata or sleep.
    monkeypatch.setattr(significance,"record_experience_marker",lambda *a,**kw:None)
    with available_cognitive_slots(dsn,user,instance,1):
        conv,sources,lease=stage(dsn,user,instance,IMPORTANT)
        source=sources[0]
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("""INSERT INTO memory_experience_markers(
                   source_turn_id,user_id,instance_id,conversation_id,source_event_id,marked_at,attention_until)
                   VALUES (%s,%s,%s,%s,%s,statement_timestamp()-interval '192 hours',
                           statement_timestamp()-interval '24 hours')""",
                         (source.turn_id,user,instance,conv,source.event_id))
        marker,=markers(dsn,user,instance)
        assert marker.attention=="AGED" and marker.significance=="USER_MARKED_IMPORTANT"
        assert markers(dsn,user,instance,active_only=True)==()
        work=call(dsn,prepare_cognition,lease)
        assert not work.turns[0]["experience_attention"]
        assert work.turns[0]["source_eligible"] and work.turns[0]["input_text"]==IMPORTANT
        call(dsn,commit_cognition,lease,work,CognitiveDraft(),"significance-fixture")
        from test_p2f12_contextual_expectations import EmptyModel
        assert call(dsn,run_cognition,lease,EmptyModel()).replayed


def test_prepared_cognitive_marker_cannot_survive_source_revocation(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        conv,sources,lease=stage(dsn,user,instance,IMPORTANT)
        saved=link_memory(dsn,user,instance,conv,sources[0].turn_id)
        work=call(dsn,prepare_cognition,lease)
        assert work.turns[0]["experience_attention"]
        invalidate(dsn,user,instance,saved,"RETRACT")
        with pytest.raises(StaleCognition):
            call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",(conv,)).fetchone()==(0,)
        assert markers(dsn,user,instance)==()
        # This test calls commit_cognition directly, so it still owns a
        # live lease after the expected rejection. Complete cooperative
        # cancellation rather than leaving the fixture to reap a live claim.
        from persona_agent.operation_control import acknowledge_cancel
        from test_phase2_cognition import cancel_pending
        cancel_pending(dsn,user,lease.operation_id)
        call(dsn,acknowledge_cancel,lease)
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT status FROM operations WHERE operation_id=%s",
                                (lease.operation_id,)).fetchone()==("CANCELLED",)
            assert conn.execute("SELECT count(*) FROM operation_resource_reservations WHERE operation_id=%s",
                                (lease.operation_id,)).fetchone()==(0,)


def test_significant_episode_only_breaks_relevant_eligible_candidate_ties(dsn):
    from test_mvp05c3_entity_evidence import _episode
    from persona_agent.meta_episodic import prepare_pattern_candidates
    user,instance,_=seed(dsn)
    source,important=_episode(dsn,user,instance,"This experience is important to me: I had a picnic.")
    _,newer=_episode(dsn,user,instance,"I had a picnic on Tuesday.")
    _,newest=_episode(dsn,user,instance,"I had a picnic on Wednesday.")
    _,unrelated=_episode(dsn,user,instance,"I bought a camera.")
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        conn.execute("UPDATE memory_episodes SET topic='camera',description='User bought a camera' WHERE episode_id=%s",(unrelated,))
        with conn.transaction(),conn.cursor() as cur:
            candidates,_=prepare_pattern_candidates(cur,user,instance,(
                {"input_text":"picnic","created_at":datetime.now(timezone.utc)},))
    assert tuple(c.episode_id for c in candidates)==(important,newest,newer)
    saved=link_memory(dsn,user,instance,source.conversation_id,source.turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.transaction(),conn.cursor() as cur:
            candidates,_=prepare_pattern_candidates(cur,user,instance,(
                {"input_text":"picnic","created_at":datetime.now(timezone.utc)},))
    assert tuple(c.episode_id for c in candidates)==(newest,newer)


def test_withdrawn_latest_question_suppresses_gap_without_older_fallback(dsn):
    user,instance,_=seed(dsn)
    _,old=_ask(dsn,user,instance,"What car do I drive?")
    latest,answer=_ask(dsn,user,instance,"What car do I drive?")
    gap,=gaps(dsn,user,instance,open_only=True)
    assert gap.response_id==answer.response_id and gap.gap_state=="OPEN"
    saved=link_memory(dsn,user,instance,latest.conversation_id,latest.turn_id)
    invalidate(dsn,user,instance,saved,"RETRACT")
    assert gaps(dsn,user,instance,open_only=True)==()
    assert gaps(dsn,user,instance,open_only=True,attention_only=True)==()
    history=gaps(dsn,user,instance)
    assert [r.gap_state for r in history]==["UNAVAILABLE","SUPERSEDED"]
    assert not history[0].request_current and history[1].response_id==old.response_id
    _,again=_ask(dsn,user,instance,"What car do I drive?")
    assert gaps(dsn,user,instance,open_only=True)[0].response_id==again.response_id


def test_awaiting_clarification_suppresses_repeat_until_source_invalidates(dsn):
    from test_mvp05c1_entity_continuity import _register
    user,instance,_=seed(dsn)
    _register(dsn,user,instance,"Alex","FRIEND")
    _register(dsn,user,instance,"Alex","COLLEAGUE")
    question="Who is Alex to me?"
    _,undelivered=_ask(dsn,user,instance,question,deliver=False)
    assert undelivered.text.endswith("?")
    assert gaps(dsn,user,instance,open_only=True,attention_only=True)[0].gap_state=="OPEN"
    asked,answer=_ask(dsn,user,instance,question)
    assert answer.text.endswith("?")
    assert gaps(dsn,user,instance,open_only=True)[0].gap_state=="AWAITING_CLARIFICATION"
    assert gaps(dsn,user,instance,open_only=True,attention_only=True)==()
    _,repeated=_ask(dsn,user,instance,question)
    assert not repeated.text.endswith("?")
    assert gaps(dsn,user,instance,open_only=True)[0].gap_state=="AWAITING_CLARIFICATION"
    saved=link_memory(dsn,user,instance,asked.conversation_id,asked.turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    assert gaps(dsn,user,instance,open_only=True,attention_only=True)[0].gap_state=="OPEN"
    _,renewed=_ask(dsn,user,instance,question)
    assert renewed.text.endswith("?")
    assert gaps(dsn,user,instance,open_only=True)[0].gap_state=="AWAITING_CLARIFICATION"


def test_current_review_feedback_prioritizes_attention_but_audit_survives_expiry(dsn):
    user,instance,_=seed(dsn)
    req,answer=_ask(dsn,user,instance,"What car do I drive?")
    command=_feedback(dsn,user,instance,req,answer,outcome="MISSED")
    call(dsn,record_retrieval_feedback,actor(user),command)
    _,other=_ask(dsn,user,instance,"What camera do I own?")
    current=gaps(dsn,user,instance,open_only=True,attention_only=True)
    assert [(r.response_id,r.gap_state) for r in current]==[
        (answer.response_id,"REVIEW_REQUIRED"),(other.response_id,"OPEN")]
    saved=link_memory(dsn,user,instance,command.conversation_id,command.feedback_turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    current=gaps(dsn,user,instance,open_only=True,attention_only=True)
    assert [(r.response_id,r.gap_state) for r in current]==[
        (other.response_id,"OPEN"),(answer.response_id,"OPEN")]
    original=next(r for r in gaps(dsn,user,instance) if r.response_id==answer.response_id)
    assert original.feedback==("MISSED",) and original.gap_state=="OPEN"
