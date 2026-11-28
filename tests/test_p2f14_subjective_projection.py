"""Actual owner, model-context and response delivery boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from threading import Event
from time import monotonic,sleep
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.conversation import (
    prepare_text_context,commit_text_response,acknowledge_text_delivery,lookup_response,
    respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply,StaleHistoricalContext,InvalidatedResponse
from persona_agent.contextual_expectations import read_expectation,withdraw_expectation
from persona_agent.expectation_contracts import WithdrawExpectation
from persona_agent.subjective_contracts import CancelSubjectiveIntention
from persona_agent.subjective_context import (
    inspect_subjective_intentions,cancel_subjective_intention,require_subjective_projection,
)
from persona_agent.runtime import PermissionDenied
from test_phase2_cognition import actor,call,seed
from test_p2f11_predictive_admission import set_enabled,reset
from test_p2f12_contextual_expectations import available_cognitive_slots,report,reflect
from test_p2f13_prediction_lifecycle import incoming,initialize
from test_mvp07b2_response_lineage import lineage
from workflow_evidence_helpers import link_memory,invalidate


def prepare(dsn,user,source,**kwargs):
    return call(dsn,prepare_text_context,actor(user),source.turn_id,**kwargs)


def reply(dsn,user,context,text="Thank you for sharing that update."):
    return call(dsn,commit_text_response,actor(user),context,
                ModelReply(model_id="subjective-fixture",text=text),model_generated=True)


def history(dsn,user,instance):
    return call(dsn,inspect_subjective_intentions,actor(user),instance)


def support_intention(dsn,user,instance):
    # Prior history predicts non-completion. A matching report advances the
    # Cognitive expectation and produces a distinct conditional intention.
    initialize(dsn,user,instance,False)
    reflect(dsn,user,instance,report(2,False))
    entry,=history(dsn,user,instance)
    assert entry["effective_status"]=="REQUIRES_CONDITION_AND_SOURCE_CHECK"
    return entry


def test_distinct_histories_change_actual_context_not_persistent_mood(dsn):
    a,one,_=seed(dsn)
    b,two,_=seed(dsn)
    for user,instance,completed in ((a,one,False),(b,two,True)):
        with available_cognitive_slots(dsn,user,instance,1):
            initialize(dsn,user,instance,completed)
    outcome_a=incoming(dsn,a,one,report(0))
    outcome_b=incoming(dsn,b,two,report(0))
    observed=[]
    class Gateway:
        def generate(self,messages):
            observed.append(messages[0].content)
            return ModelReply(model_id="subjective-fixture",text="Well done.")
    with psycopg.connect(dsn,autocommit=True) as conn:
        before=conn.execute("""SELECT count(*) FROM emotion_appraisals
                               WHERE user_id=ANY(%s::uuid[])""",([a,b],)).fetchone()
    for user,outcome in ((a,outcome_a),(b,outcome_b)):
        call(dsn,respond_to_text,actor(user),outcome.turn_id,Gateway())
    assert "Optional subjective presentation" in observed[0]
    assert "reported success warmly" in observed[0]
    assert "Optional subjective presentation" not in observed[1]
    assert "more likely not" not in observed[0]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("""SELECT count(*) FROM emotion_appraisals
                               WHERE user_id=ANY(%s::uuid[])""",([a,b],)).fetchone()==before


def test_conditional_intention_reserved_once_not_forecast_or_delivery_claim(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        intention=support_intention(dsn,user,instance)
        unrelated=incoming(dsn,user,instance,report(1,False,activity="reading practice"))
        assert prepare(dsn,user,unrelated).subjective_projection is None
        source=incoming(dsn,user,instance,report(1,False))
        ctx=prepare(dsn,user,source)
        projection=ctx.subjective_projection
        assert ctx.prediction_comparison.classification=="EXPECTED"
        assert projection.kind=="SUPPORT" and projection.intention_id==intention["intention_id"]
        assert projection.expectation_revision_id==intention["expectation_revision_id"]
        assert "one small practical adjustment" in ctx.messages[0].content
        result=reply(dsn,user,ctx)
        assert lineage(dsn,result.response_id)["subjective_intention_id"]==intention["intention_id"]
        entry,=history(dsn,user,instance)
        assert entry["effective_status"]=="RESERVED" and not entry["already_asked"]
        # The reply may or may not contain the suggested approach. ACK is
        # not proof it asked a question, so the owner keeps already_asked false.
        call(dsn,acknowledge_text_delivery,actor(user),result.response_id)
        later=incoming(dsn,user,instance,report(0,False))
        assert prepare(dsn,user,later).subjective_projection is None
        assert not history(dsn,user,instance)[0]["already_asked"]
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("""SELECT count(*) FROM cognitive_predictions
                                  WHERE user_id=%s AND instance_id=%s""",
                                (user,instance)).fetchone()==(0,)


@pytest.mark.parametrize("boundary,action",[
    ("COMMIT","REVISE"),("COMMIT","COUNTERREPORT"),("COMMIT","RETRACT"),
    ("OFFER","DISABLE"),("OFFER","WITHDRAW"),("ACK","REVISE"),
    ("REPLAY","RESET"),("REPLAY","COUNTERREPORT"),("ACK","BOUNDARY"),
])
def test_actual_projection_invalidates_at_output_boundaries(dsn,boundary,action):
    from persona_agent.application_auth import authenticate_application
    from persona_agent.application_host import _offer
    from test_mvp07a_application import provision
    from test_phase2_affect import preference
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        e=initialize(dsn,user,instance,False)
        if action=="RETRACT":
            sid=e.source_turn_ids[0]
            with psycopg.connect(dsn,autocommit=True) as conn:
                cid=conn.execute("SELECT conversation_id FROM conversation_turns WHERE turn_id=%s",(sid,)).fetchone()[0]
            saved=link_memory(dsn,user,instance,cid,sid)
        credential=provision(dsn,user,instance)
        identity=call(dsn,authenticate_application,credential.token)
        outcome=incoming(dsn,user,instance,report(0),credential.conversation_id)
        context=call(dsn,prepare_text_context,identity.actor,outcome.turn_id)
        p=context.subjective_projection
        assert p.kind=="MISMATCH" and p.expectation_revision_id==e.revision_id
        assert context.predictive_fence==p.fence
        generated=ModelReply(model_id="subjective-fixture",text="Good work.")
        result=None
        if boundary!="COMMIT":
            result=call(dsn,commit_text_response,identity.actor,context,generated,model_generated=True)
            row=lineage(dsn,result.response_id)
            assert row["current"] and row["subjective_expectation_id"]==e.revision_id
            assert row["subjective_outcome_turn_id"]==outcome.turn_id
            assert not row["presentation_only"]
            if boundary in ("ACK","REPLAY"):
                assert call(dsn,_offer,identity,result)["type"]=="reply"
            if boundary=="REPLAY":
                call(dsn,acknowledge_text_delivery,identity.actor,result.response_id)
        if action=="REVISE":
            reflect(dsn,user,instance,report(1,False))
            assert call(dsn,read_expectation,actor(user),instance,"daily walk").version==e.version+1
        elif action=="COUNTERREPORT":
            incoming(dsn,user,instance,report(0,False))
        elif action=="RETRACT":
            invalidate(dsn,user,instance,saved,"RETRACT")
        elif action=="DISABLE":
            set_enabled(dsn,user,instance,1,False)
        elif action=="RESET":
            reset(dsn,user,instance,1)
        elif action=="BOUNDARY":
            boundary_source=incoming(dsn,user,instance,"Please do not send proactive messages.")
            preference(dsn,user,instance,boundary_source,"Please do not send proactive messages",
                       key="PROACTIVE_MESSAGES",value="DISALLOW")
        else:
            call(dsn,withdraw_expectation,actor(user),WithdrawExpectation(
                instance_id=instance,revision_id=e.revision_id,expected_version=e.version,
                expected_generation=1,idempotency_key="withdraw"))
        if boundary=="COMMIT":
            with pytest.raises(StaleHistoricalContext):
                call(dsn,commit_text_response,identity.actor,context,generated,model_generated=True)
            assert call(dsn,lookup_response,actor(user),outcome.turn_id) is None
            fresh=call(dsn,prepare_text_context,identity.actor,outcome.turn_id)
            assert fresh.subjective_projection is None and fresh.predictive_fence is None
            assert fresh.messages[-1].content==report(0)
            call(dsn,commit_text_response,identity.actor,fresh,generated,model_generated=True)
        else:
            assert not lineage(dsn,result.response_id)["current"]
            with pytest.raises(InvalidatedResponse):
                call(dsn,_offer,identity,result)
            if boundary in ("ACK","REPLAY"):
                with pytest.raises(InvalidatedResponse):
                    call(dsn,acknowledge_text_delivery,identity.actor,result.response_id)
            with pytest.raises(InvalidatedResponse):
                call(dsn,lookup_response,actor(user),outcome.turn_id)
            if action=="DISABLE":
                set_enabled(dsn,user,instance,2,True)
                with pytest.raises(InvalidatedResponse):
                    call(dsn,lookup_response,actor(user),outcome.turn_id)


@pytest.mark.parametrize("phase",["PREPARED","GENERATED"])
def test_cancellation_of_conditional_intention_invalidates_projection(dsn,phase):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        intention=support_intention(dsn,user,instance)
        source=incoming(dsn,user,instance,report(1,False))
        ctx=prepare(dsn,user,source)
        assert ctx.subjective_projection.intention_id==intention["intention_id"]
        result=reply(dsn,user,ctx) if phase=="GENERATED" else None
        command=CancelSubjectiveIntention(instance_id=instance,intention_id=intention["intention_id"],
                    expected_generation=1,idempotency_key="cancel-intent")
        cancelled=call(dsn,cancel_subjective_intention,actor(user),command)
        assert cancelled.intention_id==intention["intention_id"]
        assert call(dsn,cancel_subjective_intention,actor(user),command).replayed
        assert history(dsn,user,instance)[0]["effective_status"]=="CANCELLED"
        if result is None:
            with pytest.raises(StaleHistoricalContext):
                reply(dsn,user,ctx)
            assert prepare(dsn,user,source).subjective_projection is None
        else:
            with pytest.raises(InvalidatedResponse):
                call(dsn,lookup_response,actor(user),source.turn_id)


def test_budget_boundary_cold_start_and_irrelevant_input_do_not_project(dsn,monkeypatch):
    from test_phase2_affect import preference
    import persona_agent.subjective_context as subjective
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance,False)
        # 512 characters alone is not a reason to suppress projection.
        # Supply real higher-priority history so the remaining room, rather
        # than the total budget, is insufficient for the optional hint.
        conversation=uuid4()
        notes=(
            "Garden notes: the soil in the shaded bed stays damp after rain. "
            "The sunny bed drains more quickly. Both beds have different soil textures.",
            "The garden has two raised beds with different soil textures. "
            "One is clay-rich and the other is sandy. The observations concern drainage.",
        )
        for note in notes:
            incoming(dsn,user,instance,note,conversation)
        source=incoming(dsn,user,instance,report(0),conversation)
        roomy=prepare(dsn,user,source)
        assert roomy.subjective_projection is not None
        assert all(any(m.role=="user" and m.content==note for m in roomy.messages) for note in notes)
        assert roomy.messages[-1].content==report(0)
        small=prepare(dsn,user,source,max_context_chars=512)
        assert small.prediction_comparison.classification=="UNEXPECTED"
        assert small.prediction_comparison.fence is not None
        assert small.messages[-1].content==report(0)
        used=sum(len(m.content) for m in small.messages)
        assert 0<=512-used<240, "fixture must actually constrain the remaining projection budget"
        assert small.subjective_projection is None and small.predictive_fence is None
        boundary=incoming(dsn,user,instance,"Please do not send proactive messages.")
        preference(dsn,user,instance,boundary,"Please do not send proactive messages",
                   key="PROACTIVE_MESSAGES",value="DISALLOW")
        after=prepare(dsn,user,source)
        assert after.subjective_projection is None
        assert "no unsolicited proactive messages" in after.messages[0].content
        def unexpected(*args,**kwargs):
            raise AssertionError("irrelevant input tried subjective retrieval")
        monkeypatch.setattr(subjective,"prepare_subjective_projection",unexpected)
        other=incoming(dsn,user,instance,"Explain soil drainage.")
        assert prepare(dsn,user,other).subjective_projection is None


def test_projection_has_live_expiry_and_never_becomes_independent_history(dsn):
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance,False)
        source=incoming(dsn,user,instance,report(0))
        context=prepare(dsn,user,source)
        p=context.subjective_projection
        assert p.expires_at<=datetime.now(timezone.utc)+timedelta(hours=1)
        expired=replace(p,expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        with pytest.raises(StaleHistoricalContext):
            reply(dsn,user,replace(context,subjective_projection=expired))
        result=reply(dsn,user,context,"Private learned presentation fixture.")
        call(dsn,acknowledge_text_delivery,actor(user),result.response_id)
        later=incoming(dsn,user,instance,"Continue.",source.conversation_id)
        fresh=prepare(dsn,user,later)
        assert "Private learned presentation fixture." not in " ".join(m.content for m in fresh.messages)
        assert report(0) in " ".join(m.content for m in fresh.messages)
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("""SELECT response_lineage_range_text(
                       %s,%s,%s,false,%s,1,2)""",
                    (result.response_id,user,instance,result.text)).fetchone()[0]!=result.text
            assert conn.execute("SELECT dialogue_response_sources(%s,%s,%s,%s)",
                    (user,instance,source.conversation_id,result.response_id)).fetchone()==(None,)


def test_counterreport_visibility_serializes_with_controlled_projection(dsn,monkeypatch):
    import persona_agent.learning_control as control
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance,False)
        source=incoming(dsn,user,instance,report(0))
        ctx=prepare(dsn,user,source)
        reached=Event()
        backend={}
        original=control.serialize_admitted_report
        def observe(cur,uid,iid):
            if uid==user:
                backend["pid"]=cur.connection.info.backend_pid
                reached.set()
            return original(cur,uid,iid)
        monkeypatch.setattr(control,"serialize_admitted_report",observe)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
                with conn.transaction(),conn.cursor() as cur:
                    require_subjective_projection(cur,user,instance,ctx.subjective_projection)
                    counter=pool.submit(incoming,dsn,user,instance,report(0,False))
                    assert reached.wait(10)
                    deadline=monotonic()+10
                    blocked=False
                    with psycopg.connect(dsn,autocommit=True) as monitor:
                        while monotonic()<deadline:
                            blocked=conn.info.backend_pid in monitor.execute(
                                "SELECT pg_blocking_pids(%s)",(backend["pid"],)).fetchone()[0]
                            if blocked or counter.done():
                                break
                            sleep(.02)
                    assert blocked,"counterreport did not wait for controlled projection"
                    require_subjective_projection(cur,user,instance,ctx.subjective_projection)
                counter.result(timeout=15)
        with pytest.raises(StaleHistoricalContext):
            reply(dsn,user,ctx)
        assert prepare(dsn,user,source).subjective_projection is None


def test_intention_inspection_and_cancellation_deny_foreign_owner(dsn):
    user,instance,_=seed(dsn)
    other,_,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        intention=support_intention(dsn,user,instance)
        with pytest.raises(PermissionDenied):
            history(dsn,other,instance)
        command=CancelSubjectiveIntention(instance_id=instance,intention_id=intention["intention_id"],
                    expected_generation=1,idempotency_key="foreign-cancel")
        with pytest.raises(PermissionDenied):
            call(dsn,cancel_subjective_intention,actor(other),command)
        assert history(dsn,user,instance)[0]["status"]=="PROPOSED"


def test_out_of_order_prepared_duplicate_cannot_reserve_second_projection(dsn):
    """Both report admissions can be visible in a different commit order.

    Emulate that ordering at the immutable source fixture boundary by
    delaying the earlier ingress transaction. Only the exact prior
    expectation/occasion can reserve presentation, regardless of report ID.
    """
    import persona_agent.learning_control as control
    user,instance,_=seed(dsn)
    with available_cognitive_slots(dsn,user,instance,1):
        initialize(dsn,user,instance,False)
        at_gate=Event()
        release=Event()
        selected=[]
        original=control.serialize_admitted_report
        def pause_first(cur,uid,iid):
            if uid==user and not selected:
                selected.append(True)
                at_gate.set()
                assert release.wait(20)
            return original(cur,uid,iid)
        # Bind the earlier report while it remains uncommitted. The later
        # report can commit first and reserve its presentation.
        from unittest.mock import patch
        with patch.object(control,"serialize_admitted_report",pause_first):
            with ThreadPoolExecutor(max_workers=1) as pool:
                earlier=pool.submit(incoming,dsn,user,instance,report(0))
                try:
                    assert at_gate.wait(10)
                    later=incoming(dsn,user,instance,report(0))
                    prepared=prepare(dsn,user,later)
                    assert prepared.subjective_projection is not None
                    first=reply(dsn,user,prepared)
                finally:
                    release.set()
                previous=earlier.result(timeout=15)
        assert not lineage(dsn,first.response_id)["current"]
        # The older timestamp is now eligible for comparison, but cannot
        # reuse the same prior expectation/occasion presentation slot.
        fresh=prepare(dsn,user,previous)
        assert fresh.prediction_comparison.classification=="UNEXPECTED"
        assert fresh.subjective_projection is None
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("""SELECT count(*) FROM conversation_response_lineage
                                   WHERE user_id=%s AND instance_id=%s AND subjective_expectation_id IS NOT NULL""",
                                (user,instance)).fetchone()==(1,)
