"""MVP-05C.3: explicit entity evidence, directed relations and scoped history."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest

from persona_agent.entity_reference import entity_history_reference
from persona_agent.entity_evidence_contracts import (
    LinkEntityEvidenceCommand,SetEntityRelationshipCommand,
)


@pytest.mark.parametrize("text,name,relation,adjacent",[
    ("What do you remember about my friend Alex?","alex","FRIEND",False),
    ("What do you remember about my colleague O’Neil?","o'neil","COLLEAGUE",False),
    ("What do you remember about the entity Alex?","alex",None,False),
    ("What else do you remember about them?",None,None,True),
    ("What else do you remember about it?",None,None,True),
])
def test_explicit_entity_history_reference(text,name,relation,adjacent):
    reference=entity_history_reference(text)
    assert (reference.name_key,reference.relation,reference.adjacent)==(name,relation,adjacent)


@pytest.mark.parametrize("text",[
    "What do you remember about Alex?",
    "What do you remember about nginx?",
    "What do you remember about my favorite drink?",
    "What did I say about my colleague Alex yesterday?",
    '"What do you remember about my friend Alex?"',
    "What do you remember about my friend Alex or Sam?",
    "What do you remember about my friend Alex? And Sam?",
])
def test_generic_and_compound_requests_keep_existing_routing(text):
    assert entity_history_reference(text) is None


def _helpers():
    pytest.importorskip("psycopg")
    from test_mvp05c1_entity_continuity import _helpers as h,_register,_amend,_ask,GuessingGateway
    actor,call,seed,turn=h()
    return actor,call,seed,turn,_register,_amend,_ask,GuessingGateway


def _memory(dsn,user,instance,text,predicate,value):
    from persona_agent.memory import admit_memory
    from test_phase2_memory import candidate
    actor,call,_,turn,*_=_helpers()
    source=turn(dsn,user,instance,uuid4(),text)
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,source.conversation_id,source,kind="FACT",
        predicate=predicate,value=value,quote=text,
    ))
    return source,saved


def _link(dsn,user,instance,entity,source,source_id,*,kind="USER_MEMORY",
          memory_version=1,entity_version=1,key=None):
    from persona_agent.entity_evidence import link_entity_evidence
    actor,call,*_=_helpers()
    cmd=LinkEntityEvidenceCommand(
        instance_id=instance,expected_generation=1,
        conversation_id=source.conversation_id,evidence_turn_id=source.turn_id,
        entity_id=entity,expected_entity_version=entity_version,
        source_type=kind,source_id=source_id,expected_memory_version=memory_version,
        idempotency_key=key or str(uuid4()),
    )
    return cmd,call(dsn,link_entity_evidence,actor(user),cmd)


def _episode(dsn,user,instance,text):
    """Materialize a valid existing episode fixture; test binding, not summarization."""
    import psycopg
    _,_,_,turn,*_=_helpers()
    source=turn(dsn,user,instance,uuid4(),text)
    summary,episode=uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            sequence,at=conn.execute(
                "SELECT sequence,created_at FROM conversation_turns WHERE turn_id=%s",
                (source.turn_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO conversation_summaries(
                       summary_id,conversation_id,user_id,instance_id,start_sequence,end_sequence,
                       first_turn_id,last_turn_id,source_digest,summary_text,model_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fixture-summary')""",
                (summary,source.conversation_id,user,instance,sequence,sequence,
                 source.turn_id,source.turn_id,"a"*64,"User discussed a picnic."),
            )
            conn.execute(
                """INSERT INTO memory_episodes(
                       episode_id,summary_id,conversation_id,user_id,instance_id,
                       source_turn_id,source_sequence,source_quote,source_turn_at,
                       topic,description,confidence
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'picnic','User reported a picnic',0.8)""",
                (episode,summary,source.conversation_id,user,instance,
                 source.turn_id,sequence,text,at),
            )
    return source,episode


def _relation(dsn,user,instance,subject,obj,text,*,action="REGISTER",relation="WORKS_WITH",
              version=0,subject_version=1,object_version=1,key=None):
    from persona_agent.entity_evidence import set_entity_relationship
    actor,call,_,turn,*_=_helpers()
    source=turn(dsn,user,instance,uuid4(),text)
    cmd=SetEntityRelationshipCommand(
        instance_id=instance,expected_generation=1,conversation_id=source.conversation_id,
        evidence_turn_id=source.turn_id,idempotency_key=key or str(uuid4()),
        subject_entity_id=subject,object_entity_id=obj,expected_subject_version=subject_version,
        expected_object_version=object_version,expected_version=version,action=action,
        relation=relation,
    )
    return source,cmd,call(dsn,set_entity_relationship,actor(user),cmd)


def test_scoped_exact_bindings_and_same_name_history(dsn):
    from persona_agent.entity_evidence import link_entity_evidence
    from persona_agent.entity_history import read_entity_history
    from persona_agent.conversation import prepare_text_context
    actor,call,seed,turn,register,amend,ask,Gateway=_helpers()
    user,instance,other=seed(dsn,alternative=True)
    _,_,friend=register(dsn,user,instance,"Alex","FRIEND")
    _,_,colleague=register(dsn,user,instance,"Alex","COLLEAGUE")
    source,saved=_memory(dsn,user,instance,"I went hiking with my friend Alex.","friend_activity","hiking")
    unrelated,other_saved=_memory(
        dsn,user,instance,"I went bowling with my colleague Alex.","colleague_activity","bowling",
    )
    unlinked,_=_memory(dsn,user,instance,"I played chess with my friend Alex.","another_activity","chess")
    cmd,binding=_link(dsn,user,instance,friend.entity_id,source,saved.memory_id)
    assert call(dsn,link_entity_evidence,actor(user),cmd).replayed
    assert call(dsn,link_entity_evidence,actor(user),cmd.model_copy(
        update={"idempotency_key":"second-link-command"},
    )).link_id==binding.link_id
    _link(dsn,user,instance,colleague.entity_id,unrelated,other_saved.memory_id)
    ep_source,episode=_episode(dsn,user,instance,"I had a picnic with my friend Alex.")
    _link(dsn,user,instance,friend.entity_id,ep_source,episode,kind="EPISODE",memory_version=None)
    view=call(dsn,read_entity_history,actor(user),instance,friend.entity_id)
    assert {h.source_id for h in view.evidence}=={saved.memory_id,episode}
    assert {h.epistemic_status for h in view.evidence}=={"USER_REPORTED","MODEL_INTERPRETED"}
    assert call(dsn,read_entity_history,actor(user),other,friend.entity_id) is None
    stranger,stranger_instance,_=seed(dsn)
    assert call(dsn,read_entity_history,actor(stranger),stranger_instance,friend.entity_id) is None
    from persona_agent.runtime import PermissionDenied
    with pytest.raises(PermissionDenied):
        call(dsn,read_entity_history,actor(stranger),instance,friend.entity_id)
    # Include a conflicting same-name recent user report in this session.
    # Explicit ID-conditioned context must not reintroduce it.
    conv=uuid4()
    turn(dsn,user,instance,conv,"My colleague Alex went bowling.")
    request=turn(dsn,user,instance,conv,"What do you remember about my friend Alex?")
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.historical_fence is not None and ctx.entity_history.status=="KNOWN"
    assert ctx.entity_history.entity_id==friend.entity_id
    assert set(ctx.entity_history.source_turn_ids)=={source.turn_id,ep_source.turn_id}
    assert len(ctx.messages)==2
    prompt=ctx.messages[0].content
    assert "hiking" in prompt and "picnic" in prompt
    assert "bowling" not in prompt and "chess" not in prompt
    assert str(unrelated.turn_id) not in prompt and str(unlinked.turn_id) not in prompt
    assert "USER_REPORTED USER_MEMORY" in prompt and "MODEL_INTERPRETED EPISODE" in prompt
    assert call(dsn,read_entity_history,actor(user),instance,friend.entity_id,
                since=datetime.now(timezone.utc)+timedelta(days=1)).evidence==()


@pytest.mark.parametrize("text",[
    "I went hiking with Alex.",
    "I went hiking with my colleague Alex.",
    "I went hiking with my friend Alex Smith.",
    "I have a password for my friend Alex.",
])
def test_binding_requires_qualified_nonsensitive_source(dsn,text):
    from persona_agent.entity_contracts import EntityConflict
    _,_,seed,_,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,saved=register(dsn,user,instance,"Alex","FRIEND")
    source,memory=_memory(dsn,user,instance,text,"activity","hiking")
    with pytest.raises(EntityConflict):
        _link(dsn,user,instance,saved.entity_id,source,memory.memory_id)


def test_duplicate_qualified_names_cannot_be_bound_by_spelling(dsn):
    from persona_agent.entity_contracts import EntityConflict
    _,_,seed,_,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,one=register(dsn,user,instance,"Alex","FRIEND")
    register(dsn,user,instance,"Alex","FRIEND")
    source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","activity","hiking")
    with pytest.raises(EntityConflict):
        _link(dsn,user,instance,one.entity_id,source,memory.memory_id)


def test_memory_correction_invalidates_binding_without_relink(dsn):
    from persona_agent.memory import amend_memory
    from persona_agent.entity_history import read_entity_history
    from persona_agent.runtime import VersionConflict
    from test_phase2_memory import correction
    actor,call,seed,turn,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,entity=register(dsn,user,instance,"Alex","FRIEND")
    source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","activity","hiking")
    _link(dsn,user,instance,entity.entity_id,source,memory.memory_id)
    text="Actually, I went cycling with my friend Alex."
    newer=turn(dsn,user,instance,uuid4(),text)
    call(dsn,amend_memory,actor(user),correction(
        instance,newer.conversation_id,newer,memory.memory_id,value="cycling",quote=text,
    ))
    assert call(dsn,read_entity_history,actor(user),instance,entity.entity_id).evidence==()
    with pytest.raises(VersionConflict):
        _link(dsn,user,instance,entity.entity_id,source,memory.memory_id)
    _link(dsn,user,instance,entity.entity_id,newer,memory.memory_id,memory_version=2)
    hits=call(dsn,read_entity_history,actor(user),instance,entity.entity_id).evidence
    assert [(h.excerpt,h.record_version,h.source_turn_id) for h in hits]==[
        ("cycling",2,newer.turn_id),
    ]


def test_relationship_correction_retraction_and_directed_read(dsn):
    import psycopg
    from persona_agent.entity_evidence import set_entity_relationship
    from persona_agent.entity_history import read_entity_history
    from persona_agent.runtime import VersionConflict
    actor,call,seed,_,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,one=register(dsn,user,instance,"Alex","FRIEND")
    _,_,two=register(dsn,user,instance,"Alex","COLLEAGUE")
    _,_,third=register(dsn,user,instance,"Sam","SIBLING")
    first,cmd,saved=_relation(dsn,user,instance,one.entity_id,two.entity_id,
                              "My friend Alex works with my colleague Alex.")
    assert call(dsn,set_entity_relationship,actor(user),cmd).replayed
    for endpoint in (one,two):
        view=call(dsn,read_entity_history,actor(user),instance,endpoint.entity_id)
        assert len(view.relationships)==1
        edge=view.relationships[0]
        assert (edge.subject_entity_id,edge.object_entity_id,edge.relation)==(
            one.entity_id,two.entity_id,"WORKS_WITH",
        )
    assert call(dsn,read_entity_history,actor(user),instance,third.entity_id).relationships==()
    newer,change,corrected=_relation(
        dsn,user,instance,one.entity_id,two.entity_id,
        "Actually, my friend Alex knows my colleague Alex.",
        action="CORRECT",relation="KNOWS",version=1,
    )
    assert corrected.relationship_id==saved.relationship_id and corrected.version==2
    view=call(dsn,read_entity_history,actor(user),instance,one.entity_id)
    assert [(r.relation,r.version,r.source_turn_id) for r in view.relationships]==[
        ("KNOWS",2,newer.turn_id),
    ]
    with pytest.raises(VersionConflict):
        call(dsn,set_entity_relationship,actor(user),change.model_copy(
            update={"idempotency_key":"stale-edge-change"},
        ))
    final,_,retracted=_relation(
        dsn,user,instance,one.entity_id,two.entity_id,
        "Retract saved relationship between my friend Alex and my colleague Alex.",
        action="RETRACT",relation=None,version=2,
    )
    assert retracted.version==3
    assert call(dsn,read_entity_history,actor(user),instance,one.entity_id).relationships==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT action,relation,version,source_turn_id
               FROM memory_entity_relationship_revisions WHERE relationship_id=%s ORDER BY version""",
            (saved.relationship_id,),
        ).fetchall()==[
            ("REGISTER","WORKS_WITH",1,first.turn_id),
            ("CORRECT","KNOWS",2,newer.turn_id),("RETRACT","KNOWS",3,final.turn_id),
        ]


@pytest.mark.parametrize("text",[
    "Alex works with Sam.",
    "Maybe my friend Alex works with my colleague Sam.",
    '"My friend Alex works with my colleague Sam."',
    "My friend Alex does not work with my colleague Sam.",
    "My colleague Sam works with my friend Alex.",
])
def test_relationship_source_must_support_exact_direction_and_endpoints(dsn,text):
    from persona_agent.entity_contracts import EntityConflict
    _,_,seed,_,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,one=register(dsn,user,instance,"Alex","FRIEND")
    _,_,two=register(dsn,user,instance,"Sam","COLLEAGUE")
    with pytest.raises(EntityConflict):
        _relation(dsn,user,instance,one.entity_id,two.entity_id,text)


def test_ambiguous_unknown_and_no_evidence_history_cannot_deliver_model_guess(dsn,monkeypatch):
    from persona_agent.conversation import prepare_text_context
    from persona_agent.entity_history import FALLBACKS
    actor,call,seed,turn,register,_,ask,Gateway=_helpers()
    user,instance,_=seed(dsn)
    register(dsn,user,instance,"Alex","FRIEND")
    register(dsn,user,instance,"Alex","COLLEAGUE")
    g=Gateway()
    def forbidden(*args,**kwargs):
        raise AssertionError("unresolved entity must not search linked history")
    with monkeypatch.context() as patch:
        patch.setattr("persona_agent.entity_history._entity_candidates",forbidden)
        for text,status in (
            ("What do you remember about the entity Alex?","AMBIGUOUS"),
            ("What do you remember about my friend Sam?","UNKNOWN"),
            ("What else do you remember about them?","AMBIGUOUS"),
        ):
            _,reply=ask(dsn,user,instance,text,g)
            assert reply.text==FALLBACKS[status]
    _,reply=ask(dsn,user,instance,"What do you remember about my colleague Alex?",g)
    assert reply.text==FALLBACKS["NO_EVIDENCE"]
    request=turn(dsn,user,instance,uuid4(),"What do you remember about my friend Alex?")
    small=call(dsn,prepare_text_context,actor(user),request.turn_id,max_context_chars=512)
    assert small.messages[-1].content=="What do you remember about my friend Alex?"
    assert sum(len(m.content) for m in small.messages)<=512


def test_delivered_entity_binding_can_select_adjacent_history(dsn):
    from persona_agent.conversation import prepare_text_context
    actor,call,seed,turn,register,_,ask,Gateway=_helpers()
    user,instance,_=seed(dsn)
    _,_,entity=register(dsn,user,instance,"Alex","FRIEND")
    source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","activity","hiking")
    _link(dsn,user,instance,entity.entity_id,source,memory.memory_id)
    conv=uuid4()
    ask(dsn,user,instance,"Who is Alex to me?",Gateway(),conversation=conv)
    request=turn(dsn,user,instance,conv,"What else do you remember about them?")
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.entity_history.entity_id==entity.entity_id
    assert "hiking" in ctx.messages[0].content


@pytest.mark.parametrize("initial_lookup",[False,True])
@pytest.mark.parametrize("interruption",["NONE","UNDELIVERED","OTHER_TURN"])
def test_entity_history_responses_anchor_adjacent_followups(dsn,initial_lookup,interruption):
    from persona_agent.conversation import prepare_text_context
    from persona_agent.conversation_contracts import ModelReply
    actor,call,seed,turn,register,_,ask,Gateway=_helpers()
    user,instance,_=seed(dsn)
    _,_,friend=register(dsn,user,instance,"Alex","FRIEND")
    _,_,colleague=register(dsn,user,instance,"Alex","COLLEAGUE")
    source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","friend_activity","hiking")
    _link(dsn,user,instance,friend.entity_id,source,memory.memory_id)
    other_source,other_memory=_memory(
        dsn,user,instance,"I went bowling with my colleague Alex.","colleague_activity","bowling",
    )
    _link(dsn,user,instance,colleague.entity_id,other_source,other_memory.memory_id)

    class HistoryGateway:
        def generate(self,messages):
            return ModelReply(model_id="entity-history-test",text="You mentioned hiking together.")

    conv=uuid4()
    followup="What else do you remember about them?"
    if initial_lookup:
        ask(dsn,user,instance,"Do you remember my friend Alex?",Gateway(),conversation=conv)
    first=followup if initial_lookup else "What do you remember about my friend Alex?"
    _,reply=ask(dsn,user,instance,first,HistoryGateway(),conversation=conv,
                deliver=interruption!="UNDELIVERED")
    assert "hiking" in reply.text
    if interruption=="OTHER_TURN":
        ask(dsn,user,instance,"Explain TCP sockets.",Gateway(),conversation=conv)
    if interruption=="NONE":
        # Commit and deliver a second history reply: the next pronoun must
        # anchor to that reply's own context event, not the original lookup.
        _,reply=ask(dsn,user,instance,followup,HistoryGateway(),conversation=conv)
        assert "hiking" in reply.text
    request=turn(dsn,user,instance,conv,followup)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    if interruption=="NONE":
        assert ctx.entity_history.status=="KNOWN"
        assert ctx.entity_history.entity_id==friend.entity_id
        assert ctx.entity_history.source_turn_ids==(source.turn_id,)
        assert "hiking" in ctx.messages[0].content
        assert "bowling" not in ctx.messages[0].content
    else:
        assert ctx.entity_history.status=="AMBIGUOUS"
        assert ctx.entity_history.entity_id is None


def test_unavailable_entity_history_does_not_anchor_adjacent_followup(dsn):
    from persona_agent.conversation import prepare_text_context
    from persona_agent.entity_history import FALLBACKS
    actor,call,seed,turn,register,_,ask,Gateway=_helpers()
    user,instance,_=seed(dsn)
    register(dsn,user,instance,"Alex","FRIEND")
    conv=uuid4()
    _,reply=ask(dsn,user,instance,"What do you remember about my friend Alex?",
                Gateway(),conversation=conv)
    assert reply.text==FALLBACKS["NO_EVIDENCE"]
    request=turn(dsn,user,instance,conv,"What else do you remember about them?")
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.entity_history.status=="AMBIGUOUS"
    assert ctx.entity_history.entity_id is None


@pytest.mark.parametrize("change",["EDGE","LINK","ENDPOINT_PRIVACY","ENTITY_EXPIRY"])
def test_entity_history_fence_rejects_changed_evidence(dsn,change):
    import psycopg
    from persona_agent.conversation import prepare_text_context,commit_text_response,lookup_response
    from persona_agent.conversation_contracts import ModelReply,StaleHistoricalContext
    actor,call,seed,turn,register,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,one=register(dsn,user,instance,"Alex","FRIEND")
    _,_,two=register(dsn,user,instance,"Sam","COLLEAGUE")
    _relation(dsn,user,instance,one.entity_id,two.entity_id,"My friend Alex works with my colleague Sam.")
    if change=="ENTITY_EXPIRY":
        with psycopg.connect(dsn,autocommit=True) as conn:
            expiry=conn.execute(
                """UPDATE memory_entities SET expires_at=clock_timestamp()+interval '3 seconds'
                   WHERE entity_id=%s RETURNING expires_at""",(two.entity_id,),
            ).fetchone()[0]
    request=turn(dsn,user,instance,uuid4(),"What do you remember about my friend Alex?")
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert "works with" in ctx.messages[0].content and ctx.entity_history.status=="KNOWN"
    if change=="EDGE":
        _relation(dsn,user,instance,one.entity_id,two.entity_id,
                  "Actually my friend Alex knows my colleague Sam.",
                  action="CORRECT",relation="KNOWS",version=1)
    elif change=="LINK":
        source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","activity","hiking")
        _link(dsn,user,instance,one.entity_id,source,memory.memory_id)
    elif change=="ENDPOINT_PRIVACY":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_entities SET privacy_class='SENSITIVE' WHERE entity_id=%s",
                         (two.entity_id,))
    else:
        assert ctx.historical_fence.next_expiry==expiry
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(
                """SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM
                   (%s::timestamptz-clock_timestamp())))+0.02)""",(expiry,),
            )
    with pytest.raises(StaleHistoricalContext):
        call(dsn,commit_text_response,actor(user),ctx,
             ModelReply(model_id="stale-entity-history",text="Alex works with Sam."),
             model_generated=True)
    assert call(dsn,lookup_response,actor(user),request.turn_id) is None


def test_ineligible_entity_hides_linked_source_from_generic_recall(dsn):
    from persona_agent.memory import search_memories
    from persona_agent.memory_retrieval import search_relevant_history
    from persona_agent.entity_history import read_entity_history
    actor,call,seed,_,register,amend,*_=_helpers()
    user,instance,_=seed(dsn)
    _,_,entity=register(dsn,user,instance,"Alex","FRIEND")
    source,memory=_memory(dsn,user,instance,"I went hiking with my friend Alex.","activity","hiking")
    _link(dsn,user,instance,entity.entity_id,source,memory.memory_id)
    amend(dsn,user,instance,entity.entity_id,1,"Forget my saved entity Alex.","RETRACT")
    assert call(dsn,read_entity_history,actor(user),instance,entity.entity_id) is None
    assert call(dsn,search_memories,actor(user),instance,"hiking")==()
    assert call(dsn,search_relevant_history,actor(user),instance,"hiking")==()
