"""P2-F1.1: real Governance commands and synthetic predictive boundary fixtures.

No expectation or learned-suitability producer exists in this increment.
The response fixtures supply an explicit host-captured fence, not a model-
invented one, to exercise actual Conversation commit/offer/ACK/replay.
"""
from dataclasses import replace
from uuid import uuid4

import pytest

from persona_agent.learning_control_contracts import (
    LearningControl,SetLearningEnabled,ResetLearnedState,
)


def test_control_contract_rejects_untrusted_authority_fields():
    from pydantic import ValidationError
    base=dict(instance_id=uuid4(),expected_generation=1,expected_version=0,
              idempotency_key="enable",enabled=True)
    for extra in ({"user_id":uuid4()},{"reset_generation":9},
                  {"reset_boundary_ref":uuid4()},{"source_event_id":uuid4()},
                  {"enabled":"true"}):
        with pytest.raises(ValidationError):
            SetLearningEnabled.model_validate({**base,**extra})
    assert not LearningControl(user_id=uuid4(),instance_id=uuid4()).learning_enabled


def helpers():
    pytest.importorskip("psycopg")
    from test_phase1_dialogue import actor,call,incoming,seed
    return actor,call,incoming,seed


def set_enabled(dsn,user,instance,version,enabled,*,key=None,generation=1):
    actor,call,_,_=helpers()
    from persona_agent.learning_control import set_learning_enabled
    return call(dsn,set_learning_enabled,actor(user),SetLearningEnabled(
        instance_id=instance,expected_generation=generation,expected_version=version,
        idempotency_key=key or uuid4().hex,enabled=enabled))


def reset(dsn,user,instance,version,*,key=None,generation=1):
    actor,call,_,_=helpers()
    from persona_agent.learning_control import reset_learned_state
    return call(dsn,reset_learned_state,actor(user),ResetLearnedState(
        instance_id=instance,expected_generation=generation,expected_version=version,
        idempotency_key=key or uuid4().hex))


def capture(dsn,user,instance,*sources):
    import psycopg
    from psycopg.rows import dict_row
    from persona_agent.predictive_admission import capture_predictive_fence
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.transaction(),conn.cursor() as cur:
            return capture_predictive_fence(cur,user,instance,[s.turn_id for s in sources])


def recheck(dsn,user,instance,fence):
    import psycopg
    from psycopg.rows import dict_row
    from persona_agent.predictive_admission import require_predictive_fence
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.transaction(),conn.cursor() as cur:
            require_predictive_fence(cur,user,instance,fence)


def test_control_default_off_cas_idempotency_scope_and_audit(dsn):
    actor,call,incoming,seed=helpers()
    import psycopg
    from persona_agent.learning_control import get_learning_control,set_learning_enabled
    from persona_agent.lifecycle import TrustedPlatformActor
    from persona_agent.runtime import PermissionDenied,VersionConflict,IdempotencyConflict,StaleSelection
    user,instance,other=seed(dsn,two=True)
    foreign,_=seed(dsn)
    missing=call(dsn,get_learning_control,actor(user),instance)
    assert not missing.learning_enabled and missing.version==missing.reset_generation==0
    assert missing.source_event_id is None and missing.reset_boundary_ref is None
    with pytest.raises(PermissionDenied):
        call(dsn,get_learning_control,actor(foreign),instance)
    with pytest.raises(PermissionDenied):
        call(dsn,set_learning_enabled,TrustedPlatformActor(user_id=user,source_id="worker"),
             SetLearningEnabled(instance_id=instance,expected_generation=1,expected_version=0,
                                idempotency_key="denied",enabled=True))
    with pytest.raises(StaleSelection):
        set_enabled(dsn,user,other,0,True)
    with pytest.raises(PermissionDenied):
        set_enabled(dsn,foreign,instance,0,True)
    noop=set_enabled(dsn,user,instance,0,False,key="off")
    assert not noop.changed and noop.control==missing
    enabled=set_enabled(dsn,user,instance,0,True,key="on")
    assert enabled.changed and enabled.control.version==1
    assert enabled.control.learning_enabled and enabled.control.reset_generation==0
    repeated=set_enabled(dsn,user,instance,0,True,key="on")
    assert repeated.replayed and repeated.control==enabled.control
    assert not set_enabled(dsn,user,instance,1,True).changed
    with pytest.raises(IdempotencyConflict):
        set_enabled(dsn,user,instance,1,False,key="on")
    with pytest.raises(VersionConflict):
        set_enabled(dsn,user,instance,0,False)
    disabled=set_enabled(dsn,user,instance,1,False)
    assert disabled.control.version==2 and not disabled.control.learning_enabled
    assert call(dsn,get_learning_control,actor(user),instance)==disabled.control
    assert not call(dsn,get_learning_control,actor(user),other).learning_enabled
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute("""SELECT e.trust_level,e.payload->>'version',o.event_id
            FROM events e JOIN outbox o ON o.event_id=e.event_id
            WHERE e.user_id=%s AND e.instance_id=%s
              AND e.event_type='governance.learning.enabled_changed.v1'
            ORDER BY e.stream_sequence""",(user,instance)).fetchall()
        assert rows==[("AUTHENTICATED_USER","1",enabled.control.source_event_id),
                      ("AUTHENTICATED_USER","2",disabled.control.source_event_id)]


@pytest.mark.parametrize("enabled_at_reset",[False,True])
def test_generation_cutoff_across_sessions_and_no_disabled_catchup(dsn,enabled_at_reset):
    actor,call,incoming,seed=helpers()
    from persona_agent.learning_control import get_learning_control
    from persona_agent.conversation_contracts import StaleHistoricalContext
    user,instance=seed(dsn)
    other,other_instance=seed(dsn)
    before=incoming(dsn,user,instance,uuid4(),"I walked this morning.")
    assert capture(dsn,user,instance,before) is None
    set_enabled(dsn,user,instance,0,True)
    set_enabled(dsn,other,other_instance,0,True)
    first=incoming(dsn,user,instance,uuid4(),"I walked on Monday.")
    second=incoming(dsn,user,instance,uuid4(),"I walked on Tuesday.")
    unrelated=incoming(dsn,other,other_instance,uuid4(),"I walked on Wednesday.")
    independent=capture(dsn,other,other_instance,unrelated)
    old=capture(dsn,user,instance,first,second)
    assert old is not None and old.reset_generation==0
    assert capture(dsn,user,instance,first,before) is None
    assert capture(dsn,user,instance,first,unrelated) is None
    set_enabled(dsn,user,instance,1,False)
    paused=incoming(dsn,user,instance,uuid4(),"I walked on Thursday.")
    assert capture(dsn,user,instance,first) is None
    set_enabled(dsn,user,instance,2,True)
    fresh=capture(dsn,user,instance,first,second)
    assert fresh is not None and fresh.reset_generation==old.reset_generation
    assert fresh.control_version==3
    with pytest.raises(StaleHistoricalContext):
        recheck(dsn,user,instance,old)
    assert capture(dsn,user,instance,paused) is None
    version=3
    if not enabled_at_reset:
        set_enabled(dsn,user,instance,version,False)
        version+=1
    changed=reset(dsn,user,instance,version,key="reset")
    assert changed.control.reset_generation==1
    assert changed.control.learning_enabled==enabled_at_reset
    assert changed.control.reset_boundary_ref==changed.control.source_event_id
    assert reset(dsn,user,instance,version,key="reset").replayed
    assert call(dsn,get_learning_control,actor(user),instance)==changed.control
    if not enabled_at_reset:
        set_enabled(dsn,user,instance,version+1,True)
    later=incoming(dsn,user,instance,uuid4(),"I walked on Friday.")
    current=capture(dsn,user,instance,later)
    assert current is not None and current.reset_generation==1
    assert capture(dsn,user,instance,first) is None
    assert capture(dsn,user,instance,later,second) is None
    with pytest.raises(StaleHistoricalContext):
        recheck(dsn,user,instance,fresh)
    recheck(dsn,other,other_instance,independent)


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_source_withdrawal_during_pause_cannot_revive_learning(dsn,change):
    actor,call,incoming,seed=helpers()
    from persona_agent.conversation_contracts import StaleHistoricalContext
    from workflow_evidence_helpers import link_memory,invalidate
    user,instance=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    source=incoming(dsn,user,instance,uuid4(),"I completed a walk today.")
    saved=link_memory(dsn,user,instance,source.conversation_id,source.turn_id)
    prepared=capture(dsn,user,instance,source)
    assert prepared is not None
    set_enabled(dsn,user,instance,1,False)
    invalidate(dsn,user,instance,saved,change)
    set_enabled(dsn,user,instance,2,True)
    assert capture(dsn,user,instance,source) is None
    with pytest.raises(StaleHistoricalContext):
        recheck(dsn,user,instance,prepared)


@pytest.mark.parametrize("boundary",["COMMIT","OFFER","ACK","REPLAY"])
@pytest.mark.parametrize("action",["DISABLE","RESET"])
def test_prepared_predictive_response_is_fenced_at_real_output_boundaries(dsn,boundary,action):
    actor,call,incoming,seed=helpers()
    from persona_agent.conversation import (
        prepare_text_context,commit_text_response,acknowledge_text_delivery,lookup_response,
    )
    from persona_agent.conversation_contracts import (
        ModelReply,InvalidatedResponse,StaleHistoricalContext,
    )
    from persona_agent.application_auth import authenticate_application
    from persona_agent.application_host import _offer
    from persona_agent.learning_control import get_learning_control
    from test_mvp07a_application import provision
    from test_mvp07b2_response_lineage import lineage
    user,instance=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    source=incoming(dsn,user,instance,uuid4(),"I walked yesterday.")
    credential=provision(dsn,user,instance)
    identity=call(dsn,authenticate_application,credential.token)
    request=incoming(dsn,user,instance,credential.conversation_id,"What should we discuss?")
    context=call(dsn,prepare_text_context,identity.actor,request.turn_id,max_turns=1)
    assert context.predictive_fence is None
    fence=capture(dsn,user,instance,source)
    assert fence is not None
    prepared=replace(context,predictive_fence=fence)
    generated=ModelReply(model_id="synthetic-predictive-fixture",text="A learned walking interpretation.")
    reply=None
    if boundary!="COMMIT":
        reply=call(dsn,commit_text_response,identity.actor,prepared,generated,model_generated=True)
        assert lineage(dsn,reply.response_id)["predictive_control_version"]==fence.control_version
        assert lineage(dsn,reply.response_id)["current"]
        if boundary in ("ACK","REPLAY"):
            assert call(dsn,_offer,identity,reply)["type"]=="reply"
        if boundary=="REPLAY":
            call(dsn,acknowledge_text_delivery,identity.actor,reply.response_id)
    if action=="DISABLE":
        set_enabled(dsn,user,instance,1,False)
    else:
        reset(dsn,user,instance,1)
    if boundary=="COMMIT":
        with pytest.raises(StaleHistoricalContext):
            call(dsn,commit_text_response,identity.actor,prepared,generated,model_generated=True)
        assert call(dsn,lookup_response,actor(user),request.turn_id) is None
        return
    assert not lineage(dsn,reply.response_id)["current"]
    with pytest.raises(InvalidatedResponse):
        call(dsn,_offer,identity,reply)
    if boundary in ("ACK","REPLAY"):
        with pytest.raises(InvalidatedResponse):
            call(dsn,acknowledge_text_delivery,identity.actor,reply.response_id)
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),request.turn_id)
    if action=="DISABLE":
        set_enabled(dsn,user,instance,2,True)
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),request.turn_id)
    assert call(dsn,get_learning_control,actor(user),instance).learning_enabled


def test_cold_start_preserves_explicit_preference_and_ordinary_memory(dsn):
    actor,call,incoming,seed=helpers()
    from persona_agent.conversation import prepare_text_context
    from persona_agent.memory import admit_memory,search_memories
    from test_phase2_memory import candidate
    from test_phase2_affect import preference
    user,instance=seed(dsn)
    conv=uuid4()
    source=incoming(dsn,user,instance,conv,"My favorite drink is tea.")
    call(dsn,admit_memory,actor(user),candidate(instance,conv,source))
    pref=incoming(dsn,user,instance,conv,"Please keep replies short.")
    preference(dsn,user,instance,pref,"Please keep replies short")
    for action in ("initial","enabled","disabled","reset"):
        if action=="enabled":
            set_enabled(dsn,user,instance,0,True)
        elif action=="disabled":
            set_enabled(dsn,user,instance,1,False)
        elif action=="reset":
            reset(dsn,user,instance,2)
        request=incoming(dsn,user,instance,uuid4(),"Do you remember my favorite drink?")
        context=call(dsn,prepare_text_context,actor(user),request.turn_id)
        assert context.predictive_fence is None
        assert "favorite_drink: tea" in context.messages[0].content
        assert "response length short" in context.messages[0].content
        assert call(dsn,search_memories,actor(user),instance,"favorite drink")[0].value_text=="tea"
