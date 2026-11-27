"""MVP-07B2.8: exact source withdrawal, effective owner reads and fences."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.affect_lineage import projection_current
from persona_agent.conversation import (
    acknowledge_text_delivery,commit_text_response,lookup_response,prepare_text_context,
)
from persona_agent.conversation_contracts import (
    HistoricalReadFence,InvalidatedResponse,ModelReply,StaleHistoricalContext,
)
from persona_agent.emotion_state import read_emotion_state
from persona_agent.preference_recall import respond_with_preference_recall
from persona_agent.relationship_state import read_relationship_state
from persona_agent.runtime import PermissionDenied
from test_phase2_affect import actor,call,preference,register,seed,tick,turn
from workflow_evidence_helpers import invalidate,link_memory


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_emotion_reappraises_without_rewriting_history(dsn,change):
    consumers=register(dsn)
    user,instance,_=seed(dsn)
    conv=uuid4()
    happy=turn(dsn,user,instance,conv,"I'm happy.")
    sad=turn(dsn,user,instance,conv,"I am sad.")
    saved=link_memory(dsn,user,instance,conv,sad.turn_id)
    assert tick(dsn,consumers).emotion_applied==2
    before=call(dsn,read_emotion_state,actor(user),instance)
    assert before.tentative_user_affect=="SAD"
    assert 0<before.valence<.15
    invalidate(dsn,user,instance,saved,change)
    after=call(dsn,read_emotion_state,actor(user),instance)
    assert after.tentative_user_affect is None
    assert after.valence>before.valence+.07
    assert after.version==before.version==2
    assert after.last_source_at==before.last_source_at
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM emotion_appraisals
               WHERE user_id=%s AND instance_id=%s AND applied""",
            (user,instance),
        ).fetchone()[0]==2
        assert conn.execute(
            """SELECT valence FROM persona_emotion_state
               WHERE user_id=%s AND instance_id=%s""",
            (user,instance),
        ).fetchone()[0]<.15
    turn(dsn,user,instance,conv,"Thank you.")
    assert tick(dsn,consumers).emotion_applied==1
    current=call(dsn,read_emotion_state,actor(user),instance)
    assert current.valence>after.valence+.07
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT evidence_revision IS NOT NULL FROM persona_emotion_state
               WHERE user_id=%s AND instance_id=%s""",
            (user,instance),
        ).fetchone()[0]


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_relationship_reappraises_feedback_not_interaction_history(dsn,change):
    consumers=register(dsn)
    user,instance,_=seed(dsn)
    conv=uuid4()
    turn(dsn,user,instance,conv,"Thank you.")
    feedback=turn(dsn,user,instance,conv,"That was not helpful.")
    saved=link_memory(dsn,user,instance,conv,feedback.turn_id)
    tick(dsn,consumers)
    before=call(dsn,read_relationship_state,actor(user),instance)
    assert before.rapport==pytest.approx(-.02)
    assert before.interaction_count==2
    invalidate(dsn,user,instance,saved,change)
    after=call(dsn,read_relationship_state,actor(user),instance)
    assert after.rapport==pytest.approx(.06)
    assert after.interaction_count==before.interaction_count
    assert after.familiarity==before.familiarity
    assert after.version==before.version
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT feedback FROM relationship_observations
               WHERE source_turn_id=%s""",(feedback.turn_id,),
        ).fetchone()[0]=="NEGATIVE"
    turn(dsn,user,instance,conv,"Thank you.")
    tick(dsn,consumers)
    assert call(dsn,read_relationship_state,actor(user),instance).rapport==pytest.approx(.12)


def test_delayed_ineligible_source_never_admits_affect_or_preference(dsn):
    consumers=register(dsn)
    user,instance,_=seed(dsn)
    conv=uuid4()
    feedback=turn(dsn,user,instance,conv,"That was not helpful.")
    saved=link_memory(dsn,user,instance,conv,feedback.turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    result=tick(dsn,consumers)
    assert result.emotion_applied==0
    assert result.relationship_recorded==1
    assert call(dsn,read_emotion_state,actor(user),instance).version==0
    rel=call(dsn,read_relationship_state,actor(user),instance)
    assert rel.interaction_count==1 and rel.rapport==0


def test_preference_invalidation_does_not_restore_superseded_value(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    old=turn(dsn,user,instance,conv,"Please keep replies short.")
    preference(dsn,user,instance,old,"Please keep replies short")
    latest=turn(dsn,user,instance,conv,"Please give detailed replies.")
    preference(dsn,user,instance,latest,"Please give detailed replies",version=1,value="DETAILED")
    saved=link_memory(dsn,user,instance,conv,latest.turn_id)
    question=turn(dsn,user,instance,uuid4(),"What is my current response length preference?")
    reply=call(dsn,respond_with_preference_recall,actor(user),question.turn_id)
    assert "detailed" in reply.text.lower()
    invalidate(dsn,user,instance,saved,"EXPIRE")
    assert call(dsn,read_relationship_state,actor(user),instance).preferences==()
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    next_question=turn(dsn,user,instance,uuid4(),"What is my current response length preference?")
    answer=call(dsn,respond_with_preference_recall,actor(user),next_question.turn_id)
    assert "don't have an active saved" in answer.text
    with pytest.raises(PermissionDenied):
        preference(dsn,user,instance,latest,"Please give detailed replies",version=2,value="DETAILED")
    replacement=turn(dsn,user,instance,conv,"Please use normal length replies.")
    assert preference(dsn,user,instance,replacement,"Please use normal length replies",
                      version=2,value="NORMAL").version==3
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT version FROM relationship_preference_revisions
               WHERE user_id=%s AND instance_id=%s ORDER BY version""",
            (user,instance),
        ).fetchall()==[(1,),(2,),(3,)]


def test_invalidated_pending_preference_is_not_admitted_by_worker(dsn):
    consumers=register(dsn)
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"Please don't use emojis.")
    saved=link_memory(dsn,user,instance,conv,source.turn_id)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    assert tick(dsn,consumers).relationship_recorded==1
    assert call(dsn,read_relationship_state,actor(user),instance).preferences==()


@pytest.mark.parametrize("commit_before_invalidation",[False,True])
def test_cross_session_affect_context_has_commit_and_delivery_fence(dsn,commit_before_invalidation):
    consumers=register(dsn)
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I'm excited.")
    saved=link_memory(dsn,user,instance,conv,source.turn_id)
    tick(dsn,consumers)
    request=turn(dsn,user,instance,uuid4(),"Tell me something interesting.")
    context=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert "Tentative user-affect signal" in context.messages[0].content
    assert context.historical_fence is not None
    assert context.historical_fence.next_expiry is not None
    assert source.turn_id not in context.raw_context_fence.source_turn_ids
    generated=ModelReply(model_id="test-affect",text="You sound excited.")
    if commit_before_invalidation:
        reply=call(dsn,commit_text_response,actor(user),context,generated,model_generated=True)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    if commit_before_invalidation:
        with pytest.raises(InvalidatedResponse):
            call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    else:
        with pytest.raises(StaleHistoricalContext):
            call(dsn,commit_text_response,actor(user),context,generated,model_generated=True)
        assert call(dsn,lookup_response,actor(user),request.turn_id) is None
        fresh=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
        assert "Tentative user-affect signal" not in fresh.messages[0].content
        assert "Simulated persona expressive affect" not in fresh.messages[0].content


def test_projection_watermark_expiry_invalidates_without_revision_change():
    now=datetime.now(timezone.utc)
    state={"evidence_revision":4,"evidence_next_expiry":now-timedelta(seconds=1)}
    assert not projection_current(state,HistoricalReadFence(4,None))
    state["evidence_next_expiry"]=now+timedelta(days=1)
    assert projection_current(state,HistoricalReadFence(4,state["evidence_next_expiry"]))
    assert not projection_current(state,HistoricalReadFence(5,state["evidence_next_expiry"]))
