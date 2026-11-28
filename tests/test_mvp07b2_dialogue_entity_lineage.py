"""07B2.7: exact Dialogue and Entity evidence, real PostgreSQL."""
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.conversation import (
    acknowledge_text_delivery,commit_text_response,lookup_response,prepare_text_context,
    respond_to_text,
)
from persona_agent.conversation_contracts import (
    InvalidatedResponse,ModelIntentCandidate,ModelReply,StaleHistoricalContext,
)
from persona_agent.dialogue_contracts import (
    Attention,DialogueActKind,OfferDialogueActCommand,RegisterReferentCommand,
    Resolution,ResolveDialogueActCommand,SetThreadProgressCommand,
)
from persona_agent.dialogue_state import (
    load_active_dialogue,offer_dialogue_act,register_referent,resolve_dialogue_act,
    set_thread_progress,
)
from persona_agent.raw_context import OMITTED_USER,OMITTED_ASSISTANT,eligible_turn_ids
from test_phase1_dialogue import actor,base,call,incoming,open_for,response,revision,seed
from workflow_evidence_helpers import link_memory,invalidate


def add_referent(dsn,user,instance,conv,source,thread,label):
    ident=uuid4()
    call(dsn,register_referent,actor(user),RegisterReferentCommand(
        **base(user,instance,conv,source,revision(dsn,user,conv)),
        thread_id=thread,referent_id=ident,kind="TOPIC",label=label,
    ))
    return ident


def set_hint(dsn,user,instance,conv,source,thread,label):
    call(dsn,set_thread_progress,actor(user),SetThreadProgressCommand(
        **base(user,instance,conv,source,revision(dsn,user,conv)),
        thread_id=thread,attention=Attention.FOCUSED,resolution=Resolution.OPEN,
        next_conversational_action=label,
    ))


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_dialogue_sources_beyond_history_window_are_fenced_and_masked(dsn,change):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Orchid planning.")
    thread,_=open_for(dsn,user,instance,conv,first,"Rare orchid collection")
    ref=add_referent(dsn,user,instance,conv,first,thread,"Blue orchids")
    set_hint(dsn,user,instance,conv,first,thread,"Discuss orchid shelving")
    reply=response(dsn,user,first,"Which orchid would you choose?")
    act=uuid4()
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,act_id=act,kind=DialogueActKind.QUESTION,
        label="Which orchid would you choose?",source_response_id=reply.response_id,
    ))
    saved=link_memory(dsn,user,instance,conv,first.turn_id)
    request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
    before=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert len(before.messages)==2 and before.messages[-1].content=="Explain soil drainage."
    for expected in ("Rare orchid collection","Blue orchids","Discuss orchid shelving",
                     "Which orchid would you choose?"):
        assert expected in before.messages[0].content
    assert set(before.raw_context_fence.source_turn_ids)=={first.turn_id,request.turn_id}
    assert before.historical_fence is None
    invalidate(dsn,user,instance,saved,change)
    with pytest.raises(StaleHistoricalContext,match="raw history"):
        call(dsn,commit_text_response,actor(user),before,
             ModelReply(model_id="stale-metadata",text="Blue orchids."),model_generated=True)
    assert call(dsn,lookup_response,actor(user),request.turn_id) is None
    after=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    for old in ("Rare orchid collection","Blue orchids","Discuss orchid shelving",
                "Which orchid would you choose?"):
        assert old not in after.messages[0].content
    assert first.turn_id not in after.raw_context_fence.source_turn_ids
    view=call(dsn,load_active_dialogue,actor(user),conv)
    assert view.focus_thread_id==thread and len(view.threads)==1
    assert view.threads[0].title=="[Earlier topic omitted; source unavailable]"
    assert view.threads[0].next_conversational_action is None
    assert view.pending_acts==() and view.referents==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT title,next_conversational_action FROM dialogue_threads WHERE thread_id=%s",
                            (thread,)).fetchone()==("Rare orchid collection","Discuss orchid shelving")
        assert conn.execute("SELECT label FROM dialogue_referents WHERE referent_id=%s",
                            (ref,)).fetchone()==("Blue orchids",)
        assert conn.execute("SELECT status FROM dialogue_pending_acts WHERE act_id=%s",
                            (act,)).fetchone()==("PENDING",)
    call(dsn,resolve_dialogue_act,actor(user),ResolveDialogueActCommand(
        **base(user,instance,conv,request,revision(dsn,user,conv)),
        act_id=act,outcome=None,cancel=True,
    ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT status FROM dialogue_pending_acts WHERE act_id=%s",
                            (act,)).fetchone()==("CANCELLED",)


def test_independent_thread_and_referent_survive_invalidated_hint_source(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Garden planning.")
    thread,_=open_for(dsn,user,instance,conv,first,"Garden layout")
    add_referent(dsn,user,instance,conv,first,thread,"Raised beds")
    second=incoming(dsn,user,instance,conv,"Greenhouse planning.")
    add_referent(dsn,user,instance,conv,second,thread,"Greenhouse")
    set_hint(dsn,user,instance,conv,second,thread,"Measure greenhouse")
    saved=link_memory(dsn,user,instance,conv,second.turn_id)
    request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
    before=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert "Measure greenhouse" in before.messages[0].content
    invalidate(dsn,user,instance,saved,"EXPIRE")
    after=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert "Garden layout" in after.messages[0].content and "Raised beds" in after.messages[0].content
    assert "Greenhouse" not in after.messages[0].content and "Measure greenhouse" not in after.messages[0].content
    assert set(after.raw_context_fence.source_turn_ids)=={first.turn_id,request.turn_id}
    view=call(dsn,load_active_dialogue,actor(user),conv)
    assert view.focus_thread_id==thread and view.threads[0].title=="Garden layout"
    assert view.threads[0].next_conversational_action is None
    assert [r.label for r in view.referents]==["Raised beds"]


def test_linked_objective_cue_cannot_recover_invalidated_root(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"I want to walk more.")
    response(dsn,user,first,"A short walk would be a good starting point.")
    saved=link_memory(dsn,user,instance,conv,first.turn_id)
    request=incoming(dsn,user,instance,conv,"Could we plan the next step?")
    before=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert "walk more" in before.messages[0].content
    assert first.turn_id in before.raw_context_fence.source_turn_ids
    invalidate(dsn,user,instance,saved,"EXPIRE")
    after=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    assert "walk more" not in after.messages[0].content
    assert after.messages[-1].content=="Could we plan the next step?"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT related_signal_id IS NOT NULL FROM conversation_intent_signals WHERE turn_id=%s",
                            (request.turn_id,)).fetchone()==(True,)
        assert conn.execute("SELECT count(*) FROM current_dialogue_intents WHERE turn_id IN (%s,%s)",
                            (first.turn_id,request.turn_id)).fetchone()==(0,)


def test_model_intent_inherits_uncited_prior_raw_response_dependencies(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"I planted a rose.")
    saved=link_memory(dsn,user,instance,conv,first.turn_id)
    response(dsn,user,first,"Roses need light.")
    second=incoming(dsn,user,instance,conv,"Lately I've been thinking about a regular walking routine.")
    class Model:
        def generate_interpreted(self,messages):
            assert any(m.content=="I planted a rose." for m in messages)
            return ModelReply(model_id="intent-lineage",text="Which day would suit you?",
                intent_candidate=ModelIntentCandidate(kind="OBJECTIVE",
                    evidence_quote=messages[-1].content,confidence=0.93,reference="NONE"))
        def generate(self,messages):
            raise AssertionError("expected primary model-assisted intent")
    generated=call(dsn,respond_to_text,actor(user),second.turn_id,Model())
    call(dsn,acknowledge_text_delivery,actor(user),generated.response_id)
    before=call(dsn,load_active_dialogue,actor(user),conv)
    assert before.focus_thread_id is not None and before.pending_acts
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT epistemic_status FROM current_dialogue_intents WHERE turn_id=%s",
                            (second.turn_id,)).fetchone()==("MODEL_PROPOSED",)
    invalidate(dsn,user,instance,saved,"EXPIRE")
    after=call(dsn,load_active_dialogue,actor(user),conv)
    assert after.focus_thread_id==before.focus_thread_id
    assert after.pending_acts==() and after.threads[0].title.startswith("[Earlier topic omitted")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM current_dialogue_intents WHERE turn_id=%s",
                            (second.turn_id,)).fetchone()==(0,)
        assert conn.execute("SELECT epistemic_status FROM conversation_intent_signals WHERE turn_id=%s",
                            (second.turn_id,)).fetchone()==("MODEL_PROPOSED",)
        # Its own source is still eligible; the interpretation inherited
        # all prior raw inputs, including the uncited one.
        assert conn.execute("SELECT dialogue_turn_current(%s,%s,%s,%s)",
                            (user,instance,conv,second.turn_id)).fetchone()==(True,)


@pytest.mark.parametrize("change",["CORRECT","RETRACT","SENSITIVE","EXPIRE"])
def test_entity_report_and_recognition_lineage_masks_old_raw_and_replay(dsn,change):
    from test_mvp05c1_entity_continuity import _ask,_amend,GuessingGateway
    from persona_agent.entity_memory import find_entities
    user,instance=seed(dsn)
    conv=uuid4()
    g=GuessingGateway()
    first,reply=_ask(dsn,user,instance,"Alex is my friend.",g,conversation=conv)
    saved=call(dsn,find_entities,actor(user),instance,"Alex")[0]
    repeated,echo=_ask(dsn,user,instance,"Remember that Alex is my friend.",g,conversation=conv)
    assert "Alex as your friend" in reply.text and "Alex as your friend" in echo.text
    request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
    before=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert first.turn_id in before.raw_context_fence.source_turn_ids
    assert repeated.turn_id in before.raw_context_fence.source_turn_ids
    assert reply.text not in [m.content for m in before.messages]
    from persona_agent.summary_memory import _read_range
    from persona_agent.cognitive_engine import _range
    job=dict(user_id=user,instance_id=instance,conversation_id=conv,
             expected_generation=1,start_sequence=1,end_sequence=2)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for reader in (_read_range,_range):
                rows=reader(cur,job)
                assert [r["input_text"] for r in rows]==[
                    "Alex is my friend.","Remember that Alex is my friend."]
                assert all(r["response_text"]==OMITTED_ASSISTANT for r in rows)
    if change=="CORRECT":
        _amend(dsn,user,instance,saved.entity_id,1,"Actually Alex is my colleague.",
               "CORRECT_RELATION",relation="COLLEAGUE")
    elif change=="RETRACT":
        _amend(dsn,user,instance,saved.entity_id,1,"Forget my saved entity Alex.","RETRACT")
    else:
        with psycopg.connect(dsn,autocommit=True) as conn:
            if change=="SENSITIVE":
                conn.execute("UPDATE memory_entities SET privacy_class='SENSITIVE' WHERE entity_id=%s",
                             (saved.entity_id,))
            else:
                conn.execute("UPDATE memory_entities SET expires_at=clock_timestamp() WHERE entity_id=%s",
                             (saved.entity_id,))
    with pytest.raises(StaleHistoricalContext):
        call(dsn,commit_text_response,actor(user),before,
             ModelReply(model_id="old-entity",text="Alex is your friend."),model_generated=True)
    for source in (first,repeated):
        with pytest.raises(InvalidatedResponse):
            call(dsn,lookup_response,actor(user),source.turn_id)
    after=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert [m.content for m in after.messages[1:]]==[
        OMITTED_USER,OMITTED_ASSISTANT,OMITTED_USER,OMITTED_ASSISTANT,"Explain soil drainage."]
    assert "Alex is my friend" not in after.messages[0].content
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            assert eligible_turn_ids(cur,user,instance,conv,
                       {first.turn_id,repeated.turn_id,request.turn_id})=={request.turn_id}
            for reader in (_read_range,_range):
                rows=reader(cur,job)
                assert all(not r["source_eligible"] and r["input_text"]=="" and
                           r["response_text"] is None for r in rows)
        assert conn.execute("SELECT input_text FROM conversation_turns WHERE turn_id=%s",
                            (first.turn_id,)).fetchone()["input_text"]=="Alex is my friend."


def test_alias_addition_keeps_current_relation_source_but_fences_old_report_reply(dsn):
    from test_mvp05c1_entity_continuity import _ask,_amend,GuessingGateway
    from persona_agent.entity_memory import find_entities
    user,instance=seed(dsn)
    conv=uuid4()
    first,reply=_ask(dsn,user,instance,"Alex is my friend.",GuessingGateway(),conversation=conv)
    saved=call(dsn,find_entities,actor(user),instance,"Alex")[0]
    _amend(dsn,user,instance,saved.entity_id,1,"I also call Alex Al.","ADD_ALIAS",alias="Al")
    assert call(dsn,find_entities,actor(user),instance,"Al")[0].relation_source_turn_id==first.turn_id
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),first.turn_id)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            assert eligible_turn_ids(cur,user,instance,conv,{first.turn_id})=={first.turn_id}
    _,fresh=_ask(dsn,user,instance,"Who is Al to me?",GuessingGateway())
    assert "Alex as your friend" in fresh.text


@pytest.mark.parametrize("change",["EDGE","ENDPOINT"])
def test_entity_relationship_source_is_not_reused_after_revision_or_endpoint_expiry(dsn,change):
    from test_mvp05c1_entity_continuity import _register,_amend
    from test_mvp05c3_entity_evidence import _relation
    from persona_agent.entity_history import read_entity_history
    user,instance=seed(dsn)
    _,_,alex=_register(dsn,user,instance,"Alex","COLLEAGUE")
    _,_,sam=_register(dsn,user,instance,"Sam","COLLEAGUE")
    source,_,edge=_relation(dsn,user,instance,alex.entity_id,sam.entity_id,
        "My colleague Alex works with my colleague Sam.")
    def eligible():
        with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                return eligible_turn_ids(cur,user,instance,source.conversation_id,{source.turn_id})
    assert eligible()=={source.turn_id}
    if change=="EDGE":
        _relation(dsn,user,instance,alex.entity_id,sam.entity_id,
            "Actually my colleague Alex knows my colleague Sam.",
            action="CORRECT",relation="KNOWS",version=1)
    else:
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_entities SET expires_at=clock_timestamp() WHERE entity_id=%s",
                         (sam.entity_id,))
    assert eligible()==set()
    view=call(dsn,read_entity_history,actor(user),instance,alex.entity_id)
    if change=="EDGE":
        assert len(view.relationships)==1 and view.relationships[0].relation=="KNOWS"
    else:
        assert view.relationships==()


@pytest.mark.parametrize("prefer_hash_joins", [False, True])
def test_focused_thread_lineage_only_checks_selected_candidate(dsn,prefer_hash_joins):
    """Assert actual candidate evaluation rather than machine-dependent timing.

    The equivalent instrumented turn predicate is installed only inside a
    transaction that is rolled back, including its DDL.
    """
    from persona_agent.dialogue_integration import _focused_auto_thread

    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"I want to walk more.")
    response(dsn,user,first,"A short walk would be a good starting point.")
    second=incoming(dsn,user,instance,conv,"Switch the topic to cooking.")
    response(dsn,user,second,"We can discuss cooking.")
    other_conv=uuid4()
    other=incoming(dsn,user,instance,other_conv,"I want to learn Python.")
    response(dsn,user,other,"We can start with Python basics.")
    foreign_user,foreign_instance=seed(dsn)
    foreign=incoming(dsn,foreign_user,foreign_instance,uuid4(),"I want to exercise more.")
    response(dsn,foreign_user,foreign,"We can discuss exercise.")
    no_focus=uuid4()
    incoming(dsn,user,instance,no_focus,"Hello.")

    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            target=conn.execute(
                """SELECT d.thread_id,d.origin_turn_id
                   FROM conversations c JOIN dialogue_threads d ON d.thread_id=c.focus_thread_id
                   WHERE c.conversation_id=%s AND c.user_id=%s AND c.instance_id=%s
                     AND d.origin_kind='AUTO_SIGNAL'""",
                (conv,user,instance),
            ).fetchone()
            assert target is not None
            assert conn.execute(
                "SELECT count(*) AS n FROM dialogue_threads WHERE conversation_id=%s",
                (conv,),
            ).fetchone()["n"] >= 2
            conn.execute("SET LOCAL jit=off")
            if prefer_hash_joins:
                conn.execute("SET LOCAL enable_nestloop=off")
            for key,value in (("uid",user),("iid",instance),("cid",conv),
                              ("sid",target["origin_turn_id"]),("checks",0)):
                conn.execute("SELECT set_config(%s,%s,true)",
                             ("persona_test."+key,str(value)))
            conn.execute("""
                CREATE OR REPLACE FUNCTION dialogue_turn_current(
                    uid uuid,iid uuid,cid uuid,sid uuid
                ) RETURNS boolean LANGUAGE plpgsql AS $$
                BEGIN
                    IF sid IS NOT NULL THEN
                        IF uid IS DISTINCT FROM current_setting('persona_test.uid')::uuid
                           OR iid IS DISTINCT FROM current_setting('persona_test.iid')::uuid
                           OR cid IS DISTINCT FROM current_setting('persona_test.cid')::uuid
                           OR sid IS DISTINCT FROM current_setting('persona_test.sid')::uuid THEN
                            RAISE EXCEPTION 'eligibility evaluated outside selected focus';
                        END IF;
                        PERFORM set_config('persona_test.checks',
                            (current_setting('persona_test.checks')::int+1)::text,true);
                    END IF;
                    RETURN sid IS NOT NULL AND raw_context_sources_current(uid,iid,cid,ARRAY[sid]);
                END;
                $$;
            """)
            with conn.cursor() as cur:
                assert _focused_auto_thread(cur,user_id=user,instance_id=instance,
                                            conversation_id=conv)==target["thread_id"]
                checks=conn.execute(
                    "SELECT current_setting('persona_test.checks')::int AS n"
                ).fetchone()["n"]
                assert checks > 0
                assert _focused_auto_thread(cur,user_id=user,instance_id=instance,
                                            conversation_id=no_focus) is None
                assert conn.execute(
                    "SELECT current_setting('persona_test.checks')::int AS n"
                ).fetchone()["n"]==checks


@pytest.mark.parametrize("spare_sources", [0, 1])
def test_resolved_objective_correction_is_atomic_at_source_limit(dsn,spare_sources):
    """The original fits on its own; its correction needs one more source."""
    from persona_agent.dialogue_lineage import DialogueDependencies
    from persona_agent.intent_interpretation import resolved_objective_context_block
    from persona_agent.raw_context import MAX_RAW_SOURCES

    user,instance=seed(dsn)
    conv=uuid4()
    objective=incoming(dsn,user,instance,conv,"I want to practice walking daily.")
    response(dsn,user,objective,"We can discuss walking.")
    correction=incoming(dsn,user,instance,conv,"Actually, once per week would be better.")
    response(dsn,user,correction,"We can adjust the cadence.")
    switch=incoming(dsn,user,instance,conv,"Switch the topic to cooking.")
    response(dsn,user,switch,"We can discuss cooking.")
    request=incoming(dsn,user,instance,conv,"What about that walking goal?")
    # Simulate the raw history reservation without creating 128 extra turns.
    reserved={objective.turn_id,request.turn_id}
    reserved.update(uuid4() for _ in range(MAX_RAW_SOURCES-len(reserved)-spare_sources))
    dependencies=DialogueDependencies(reserved,None)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            kwargs=dict(user_id=user,instance_id=instance,conversation_id=conv,
                        turn_id=request.turn_id,latest_text="What about that walking goal?",
                        max_chars=1100)
            baseline=resolved_objective_context_block(cur,**kwargs)
            assert "practice walking daily" in baseline
            assert "Actually, once per week would be better." in baseline
            block=resolved_objective_context_block(cur,**kwargs,dependencies=dependencies)
            if spare_sources:
                assert "Actually, once per week would be better." in block
                assert correction.turn_id in dependencies.source_ids
                assert objective.turn_id in dependencies.source_ids
            else:
                assert block==""
                assert dependencies.source_ids==set()
                assert not dependencies.uses_broad


def test_invalidated_thread_title_keeps_independent_act_and_referent_in_prompt(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    origin=incoming(dsn,user,instance,conv,"Private original subject.")
    thread,_=open_for(dsn,user,instance,conv,origin,"Private orchid project")
    saved=link_memory(dsn,user,instance,conv,origin.turn_id)
    independent=incoming(dsn,user,instance,conv,"Blue and green are available.")
    add_referent(dsn,user,instance,conv,independent,thread,"Blue and green")
    before=call(dsn,prepare_text_context,actor(user),independent.turn_id,max_turns=1)
    assert "Private orchid project" in before.messages[0].content
    assert "Blue and green" in before.messages[0].content
    invalidate(dsn,user,instance,saved,"EXPIRE")
    # Generate the independent question after withdrawal so its own response
    # lineage cannot contain the invalid title. The authenticated explicit
    # offer uses the still-latest input, not an obsolete evidence turn.
    reply=response(dsn,user,independent,"Which color should we use?")
    act=uuid4()
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,independent,revision(dsn,user,conv)),
        thread_id=thread,act_id=act,kind=DialogueActKind.QUESTION,
        label="Which color should we use?",source_response_id=reply.response_id,
    ))
    request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
    view=call(dsn,load_active_dialogue,actor(user),conv)
    assert view.focus_thread_id==thread
    assert [a.act_id for a in view.pending_acts]==[act]
    assert [r.label for r in view.referents]==["Blue and green"]
    after=call(dsn,prepare_text_context,actor(user),request.turn_id,max_turns=1)
    prompt=after.messages[0].content
    assert "Private orchid project" not in prompt
    assert "Earlier topic omitted" in prompt
    assert "Which color should we use?" in prompt and "Blue and green" in prompt
    assert set(after.raw_context_fence.source_turn_ids)=={independent.turn_id,request.turn_id}
