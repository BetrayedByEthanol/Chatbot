"""MVP-05C.1: scoped identity, aliases, owner revisions and grounded recall."""
from uuid import uuid4

import pytest

from persona_agent.entity_reference import entity_reference,normalize_name
from persona_agent.entity_contracts import (
    RegisterEntityCommand,AmendEntityCommand,EntityConflict,
)
from persona_agent.response_decisions import decide_response
from persona_agent.entity_recall import ROUTE_ID,render_entity_hits


@pytest.mark.parametrize("text,key,kind,relation,adjacent",[
    ("Do you remember who Alex is?","alex","PERSON",None,False),
    ("Who is Alex to me?","alex","PERSON",None,False),
    ("Do you remember who O’Neil is?","o'neil","PERSON",None,False),
    ("Do you remember who O'Neil is?","o'neil","PERSON",None,False),
    ("Who is O’Neil to me?","o'neil","PERSON",None,False),
    ("Do you remember my colleague O’Neil?","o'neil","ANY","COLLEAGUE",False),
    ("Do you remember what O’Neil is?","o'neil","NONPERSON",None,False),
    ("Do you remember my colleague Alex?","alex","ANY","COLLEAGUE",False),
    ("Do you remember what Atlas is?","atlas","NONPERSON",None,False),
    ("Do you remember my favorite place Harbor?","harbor","ANY","FAVORITE_PLACE",False),
    ("How do I know them?",None,"PERSON",None,True),
    ("Do you remember what it is?",None,"NONPERSON",None,True),
])
def test_entity_reference_routes_are_exact(text,key,kind,relation,adjacent):
    reference=entity_reference(text)
    assert (reference.name_key,reference.kind,reference.relation,reference.adjacent)==(
        key,kind,relation,adjacent,
    )
    assert decide_response(text).route=="ENTITY_RECALL"


@pytest.mark.parametrize("text",[
    "Who is Alex?",
    "Do you remember Alex?",
    "Do you remember what my favorite drink is?",
    "Do you remember my preference about planning and response length?",
    "What did I say about Alex yesterday?",
    "What if I asked who Alex is to me?",
    '"Do you remember who Alex is?"',
    "Do you remember who Alex is? Also tell me the time.",
    "Remember that Alex is my friend.",
    "Do you remember who Alex or Sam is?",
    "Do you remember who my password is?",
    "‘Do you remember who O’Neil is?’",
    "Do you remember who O’Neil or Sam is?",
])
def test_unrecognized_or_compound_questions_keep_existing_routes(text):
    assert entity_reference(text) is None
    assert decide_response(text).route!="ENTITY_RECALL"


@pytest.mark.parametrize("name",["","my friend","Alex or Sam","1234","Alex\nSam","password"])
def test_bounded_name_grammar(name):
    with pytest.raises(ValueError):
        normalize_name(name)


def test_apostrophe_normalization_preserves_display_name():
    assert normalize_name("O’Neil")==normalize_name("O'Neil")=="o'neil"
    command=RegisterEntityCommand(
        instance_id=uuid4(),expected_generation=1,conversation_id=uuid4(),
        evidence_turn_id=uuid4(),name="O’Neil",relation="COLLEAGUE",
        idempotency_key="apostrophe-name",
    )
    assert command.name=="O’Neil"


def _helpers():
    pytest.importorskip("psycopg")
    from test_phase2_memory import actor,call,seed,turn
    return actor,call,seed,turn


def _register(dsn,user,instance,name,relation,*,generation=1,source=None,key=None):
    from persona_agent.entity_memory import register_entity
    from persona_agent.entity_reference import RELATIONS
    actor,call,_,turn=_helpers()
    if source is None:
        source=turn(dsn,user,instance,uuid4(),
                    f"{name} is my {RELATIONS[relation][1]}.",gen=generation)
    command=RegisterEntityCommand(
        instance_id=instance,expected_generation=generation,
        conversation_id=source.conversation_id,evidence_turn_id=source.turn_id,
        name=name,relation=relation,idempotency_key=key or str(uuid4()),
    )
    return source,command,call(dsn,register_entity,actor(user),command)


def _amend(dsn,user,instance,entity_id,version,text,action,*,relation=None,alias=None):
    from persona_agent.entity_memory import amend_entity
    actor,call,_,turn=_helpers()
    source=turn(dsn,user,instance,uuid4(),text)
    command=AmendEntityCommand(
        instance_id=instance,expected_generation=1,
        conversation_id=source.conversation_id,evidence_turn_id=source.turn_id,
        entity_id=entity_id,expected_version=version,action=action,
        relation=relation,alias=alias,idempotency_key=str(uuid4()),
    )
    return source,command,call(dsn,amend_entity,actor(user),command)


class GuessingGateway:
    def __init__(self):
        self.calls=0
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        self.calls+=1
        return ModelReply(model_id="guessing-entity-model",text="Alex is your dentist.")


def _ask(dsn,user,instance,text,gateway,*,conversation=None,forced=False,deliver=True,generation=1):
    from persona_agent.conversation import respond_to_text,acknowledge_text_delivery
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,_,turn=_helpers()
    request=turn(dsn,user,instance,conversation or uuid4(),text,gen=generation)
    reply=call(dsn,respond_to_text if forced else respond_to_user_text,
               actor(user),request.turn_id,gateway)
    if deliver:
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    return request,reply


@pytest.mark.parametrize("name",["O'Neil","O’Neil"])
@pytest.mark.parametrize("forced",[False,True])
def test_apostrophe_recall_matches_persisted_input_and_name_keys(dsn,name,forced):
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _,_,saved=_register(dsn,user,instance,name,"COLLEAGUE")
    for spelling in ("O'Neil","O’Neil"):
        hits=call(dsn,find_entities,actor(user),instance,spelling)
        assert [hit.entity_id for hit in hits]==[saved.entity_id]
    gateway=GuessingGateway()
    for spelling in ("O'Neil","O’Neil"):
        _,reply=_ask(dsn,user,instance,f"Do you remember who {spelling} is?",
                     gateway,forced=forced)
        assert f"{name} as your colleague" in reply.text
        assert "dentist" not in reply.text
        assert reply.model_id==("guessing-entity-model" if forced else ROUTE_ID)
    assert gateway.calls==(2 if forced else 0)


def test_registered_identity_alias_cross_session_and_revisioned_relation(dsn):
    import psycopg
    from persona_agent.entity_memory import register_entity,amend_entity,find_entities
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    source=turn(dsn,user,instance,uuid4(),"Remember that Alex is my friend.")
    # Ingress is not an owner mutation or automatic identity merge.
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()
    original,cmd,saved=_register(dsn,user,instance,"Alex","FRIEND",source=source)
    assert saved.version==1
    replay=call(dsn,register_entity,actor(user),cmd)
    assert replay.replayed and replay.entity_id==saved.entity_id
    # Same source cannot authorize a duplicate identity via a new key.
    with pytest.raises(EntityConflict,match="already used"):
        call(dsn,register_entity,actor(user),cmd.model_copy(
            update={"idempotency_key":"another-key"},
        ))
    alias_source,alias_cmd,alias_result=_amend(
        dsn,user,instance,saved.entity_id,1,"I also call Alex Al.","ADD_ALIAS",alias="Al",
    )
    assert alias_result.version==2
    assert call(dsn,amend_entity,actor(user),alias_cmd).replayed
    hit=call(dsn,find_entities,actor(user),instance,"AL")[0]
    assert hit.entity_id==saved.entity_id and hit.relation_source_turn_id==original.turn_id
    gateway=GuessingGateway()
    _,reply=_ask(dsn,user,instance,"Do you remember who Al is?",gateway)
    assert reply.model_id==ROUTE_ID and "Alex as your friend" in reply.text
    assert gateway.calls==0
    changed_source,change_cmd,changed=_amend(
        dsn,user,instance,saved.entity_id,2,"Actually, Al is my colleague.",
        "CORRECT_RELATION",relation="COLLEAGUE",
    )
    assert changed.version==3
    fresh=call(dsn,find_entities,actor(user),instance,"Alex")[0]
    assert fresh.entity_id==saved.entity_id and fresh.relation=="COLLEAGUE"
    assert fresh.relation_source_turn_id==changed_source.turn_id
    from persona_agent.runtime import VersionConflict
    with pytest.raises(VersionConflict):
        call(dsn,amend_entity,actor(user),change_cmd.model_copy(
            update={"idempotency_key":"stale-change"},
        ))
    request,forced=_ask(dsn,user,instance,"Who is Alex to me?",gateway,forced=True)
    assert forced.model_id=="guessing-entity-model" and gateway.calls==1
    assert "Alex as your colleague" in forced.text and "dentist" not in forced.text
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT action,version,source_turn_id FROM memory_entity_revisions
               WHERE entity_id=%s ORDER BY version""",(saved.entity_id,),
        ).fetchall()==[
            ("REGISTER",1,original.turn_id),
            ("ADD_ALIAS",2,alias_source.turn_id),
            ("CORRECT_RELATION",3,changed_source.turn_id),
        ]
        assert conn.execute(
            """SELECT payload->>'status',payload->>'entity_id',
                      payload->>'entity_version',payload->>'evidence_turn_id',
                      payload->>'model_reply_replaced'
               FROM events WHERE user_id=%s
                 AND event_type='conversation.response.entity_grounded.v1'
                 AND payload->>'response_id'=%s""",(user,str(forced.response_id)),
        ).fetchone()==("KNOWN",str(saved.entity_id),"3",str(changed_source.turn_id),"true")


def test_same_name_and_alias_collisions_require_clarification(dsn):
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _,_,friend=_register(dsn,user,instance,"Alex","FRIEND")
    _,_,colleague=_register(dsn,user,instance,"Alex","COLLEAGUE")
    assert friend.entity_id!=colleague.entity_id
    hits=call(dsn,find_entities,actor(user),instance,"alex")
    assert {hit.entity_id for hit in hits}=={friend.entity_id,colleague.entity_id}
    gateway=GuessingGateway()
    conversation=uuid4()
    _,reply=_ask(dsn,user,instance,"Do you remember who Alex is?",gateway,
                 conversation=conversation)
    assert "friend Alex" in reply.text and "colleague Alex" in reply.text and reply.text.endswith("?")
    _,ambiguous=_ask(dsn,user,instance,"How do I know them?",gateway,conversation=conversation)
    assert ambiguous.text.endswith("?")
    _,qualified=_ask(dsn,user,instance,"Do you remember my colleague Alex?",gateway,
                     conversation=conversation)
    assert "Alex as your colleague" in qualified.text
    _,pronoun=_ask(dsn,user,instance,"How do I know them?",gateway,conversation=conversation)
    assert pronoun.text==qualified.text
    _,_,sam=_register(dsn,user,instance,"Sam","FRIEND")
    _amend(dsn,user,instance,sam.entity_id,1,"I also call Sam Alex.","ADD_ALIAS",alias="Alex")
    _,collision=_ask(dsn,user,instance,"Do you remember my friend Alex?",gateway)
    assert "friend Alex" in collision.text and "friend Sam" in collision.text
    assert gateway.calls==0


def test_same_name_same_relation_is_not_silently_resolved(dsn):
    actor,call,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _register(dsn,user,instance,"Alex","FRIEND")
    _register(dsn,user,instance,"Alex","FRIEND")
    _,reply=_ask(dsn,user,instance,"Who is Alex to me?",GuessingGateway())
    assert "distinguishing detail or alias" in reply.text


@pytest.mark.parametrize("name,relation,question,expected",[
    ("Atlas","BICYCLE","Do you remember what Atlas is?","Atlas as your bicycle"),
    ("Harbor","FAVORITE_PLACE","Do you remember what Harbor is?","Harbor as your favorite place"),
])
def test_nonperson_identity_and_adjacent_reference(dsn,name,relation,question,expected):
    _,_,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _register(dsn,user,instance,name,relation)
    conversation=uuid4()
    gateway=GuessingGateway()
    _,known=_ask(dsn,user,instance,question,gateway,conversation=conversation)
    assert expected in known.text
    _,again=_ask(dsn,user,instance,"Do you remember what it is?",gateway,conversation=conversation)
    assert again.text==known.text


def test_pronouns_require_immediately_preceding_delivered_binding(dsn):
    _,_,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _register(dsn,user,instance,"Alex","FRIEND")
    conversation=uuid4()
    gateway=GuessingGateway()
    _ask(dsn,user,instance,"Do you remember who Alex is?",gateway,
         conversation=conversation,deliver=False)
    _,missing=_ask(dsn,user,instance,"How do I know them?",gateway,conversation=conversation)
    assert missing.text.endswith("?")
    _ask(dsn,user,instance,"Do you remember who Alex is?",gateway,conversation=conversation)
    _ask(dsn,user,instance,"Explain TCP sockets.",gateway,conversation=conversation)
    _,missing=_ask(dsn,user,instance,"How do I know them?",gateway,conversation=conversation)
    assert missing.text.endswith("?")
    _,cross=_ask(dsn,user,instance,"How do I know them?",gateway)
    assert cross.text.endswith("?")


def test_current_entity_state_is_read_at_commit_not_model_snapshot(dsn):
    from persona_agent.conversation import prepare_text_context,commit_text_response
    from persona_agent.conversation_contracts import ModelReply
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    _,_,saved=_register(dsn,user,instance,"Alex","FRIEND")
    question=turn(dsn,user,instance,uuid4(),"Who is Alex to me?")
    ctx=call(dsn,prepare_text_context,actor(user),question.turn_id)
    assert ctx.historical_fence is None
    _amend(dsn,user,instance,saved.entity_id,1,"Actually Alex is my colleague.",
           "CORRECT_RELATION",relation="COLLEAGUE")
    reply=call(dsn,commit_text_response,actor(user),ctx,
               ModelReply(model_id="stale-entity-model",text="Alex is your friend."),
               model_generated=True)
    assert "Alex as your colleague" in reply.text
    assert "your friend" not in reply.text


@pytest.mark.parametrize("change",["RETRACT","SENSITIVE","EXPIRED"])
def test_ineligible_entity_not_exposed_by_name_or_prior_binding(dsn,change):
    import psycopg
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_=_helpers()
    user,instance,_=seed(dsn)
    _,_,saved=_register(dsn,user,instance,"Alex","FRIEND")
    conversation=uuid4()
    gateway=GuessingGateway()
    _ask(dsn,user,instance,"Who is Alex to me?",gateway,conversation=conversation)
    if change=="RETRACT":
        _amend(dsn,user,instance,saved.entity_id,1,"Forget my saved entity Alex.","RETRACT")
    else:
        with psycopg.connect(dsn,autocommit=True) as conn:
            if change=="SENSITIVE":
                conn.execute("UPDATE memory_entities SET privacy_class='SENSITIVE' WHERE entity_id=%s",
                             (saved.entity_id,))
            else:
                conn.execute("UPDATE memory_entities SET expires_at=clock_timestamp() WHERE entity_id=%s",
                             (saved.entity_id,))
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()
    _,pronoun=_ask(dsn,user,instance,"How do I know them?",gateway,conversation=conversation)
    assert "reliable saved identification" in pronoun.text
    _,unknown=_ask(dsn,user,instance,"Who is Alex to me?",gateway,forced=True)
    assert "reliable saved identification" in unknown.text
    assert "dentist" not in unknown.text and "your friend" not in unknown.text


def test_entity_owner_scope_and_generation(dsn):
    from persona_agent.entity_memory import find_entities,register_entity,amend_entity
    from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
    from persona_agent.runtime import PermissionDenied,StaleSelection
    actor,call,seed,turn=_helpers()
    user,instance,other=seed(dsn,alternative=True)
    outsider,outsider_instance,_=seed(dsn)
    _,cmd,saved=_register(dsn,user,instance,"Alex","FRIEND")
    assert call(dsn,find_entities,actor(user),other,"Alex")==()
    assert call(dsn,find_entities,actor(outsider),outsider_instance,"Alex")==()
    with pytest.raises(PermissionDenied):
        call(dsn,find_entities,actor(outsider),instance,"Alex")
    with pytest.raises(PermissionDenied):
        call(dsn,register_entity,actor(outsider),cmd)
    foreign=turn(dsn,outsider,outsider_instance,uuid4(),"Actually Alex is my colleague.")
    bad=AmendEntityCommand(
        instance_id=outsider_instance,expected_generation=1,entity_id=saved.entity_id,
        expected_version=1,conversation_id=foreign.conversation_id,
        evidence_turn_id=foreign.turn_id,action="CORRECT_RELATION",
        relation="COLLEAGUE",idempotency_key="alien-entity",
    )
    with pytest.raises(PermissionDenied):
        call(dsn,amend_entity,actor(outsider),bad)
    pending=turn(dsn,user,instance,uuid4(),"Sam is my friend.")
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,expected_generation=1,
        idempotency_key="switch-entity",
    ))
    stale=cmd.model_copy(update={
        "name":"Sam","conversation_id":pending.conversation_id,
        "evidence_turn_id":pending.turn_id,"idempotency_key":"stale-entity",
    })
    with pytest.raises(StaleSelection):
        call(dsn,register_entity,actor(user),stale)
    _,unknown=_ask(dsn,user,other,"Who is Alex to me?",GuessingGateway(),generation=2)
    assert "reliable saved identification" in unknown.text


@pytest.mark.parametrize("text",[
    "Maybe Alex is my friend.",
    '"Alex is my friend."',
    "Alex is not my friend.",
    "What if Alex is my friend?",
    "Alex is my colleague.",
    "Alex is my friend. I am joking.",
])
def test_whole_authenticated_source_required_for_registration(dsn,text):
    from persona_agent.entity_memory import register_entity,find_entities
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    source=turn(dsn,user,instance,uuid4(),text)
    command=RegisterEntityCommand(
        instance_id=instance,expected_generation=1,conversation_id=source.conversation_id,
        evidence_turn_id=source.turn_id,name="Alex",relation="FRIEND",idempotency_key="bad-source",
    )
    with pytest.raises(EntityConflict):
        call(dsn,register_entity,actor(user),command)
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()


def test_registered_sources_not_reintroduced_via_generic_raw_recall(dsn):
    from persona_agent.conversation import (
        acknowledge_text_delivery,prepare_text_context,respond_to_text,
    )
    from test_phase1_conversation import FakeGateway
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    source,_,saved=_register(dsn,user,instance,"Alex","FRIEND")
    initial=call(dsn,respond_to_text,actor(user),source.turn_id,FakeGateway("Understood."))
    call(dsn,acknowledge_text_delivery,actor(user),initial.response_id)
    _amend(dsn,user,instance,saved.entity_id,1,"Actually Alex is my colleague.",
           "CORRECT_RELATION",relation="COLLEAGUE")
    request=turn(dsn,user,instance,uuid4(),"What did I say about Alex?")
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert "Alex is my friend." not in ctx.messages[0].content
    assert str(source.turn_id) not in ctx.messages[0].content
