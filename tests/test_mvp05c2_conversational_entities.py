"""MVP-05C.2: authenticated natural reports and delivered entity choices."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from persona_agent.entity_reference import entity_report,entity_clarification_relation
from persona_agent.response_decisions import decide_response


@pytest.mark.parametrize("text,name,relation,action",[
    ("Alex is my friend.","Alex","FRIEND","REPORT"),
    ("Remember that Alex is my colleague.","Alex","COLLEAGUE","REPORT"),
    ("Remember that o’neil is my sibling.","o’neil","SIBLING","REPORT"),
    ("Another Alex is my colleague.","Alex","COLLEAGUE","DISTINCT"),
    ("Actually, Alex is my colleague.","Alex","COLLEAGUE","CORRECT"),
    ("Atlas is my bicycle.","Atlas","BICYCLE","REPORT"),
    ("Harbor is my favorite place.","Harbor","FAVORITE_PLACE","REPORT"),
])
def test_whole_entity_report_grammar(text,name,relation,action):
    report=entity_report(text)
    assert (report.name,report.relation,report.action)==(name,relation,action)
    assert decide_response(text).route=="ENTITY_REPORT"


@pytest.mark.parametrize("text",[
    "Maybe Alex is my friend.","Perhaps Alex is my friend.",
    "Alex is not my friend.","Alex is my friend and colleague.",
    '"Alex is my friend."',"Imagine Alex is my friend.",
    "What if Alex is my friend?","Alex is my friend?",
    "Alex is my friend. Just kidding.","No Alex is my friend.",
    "Remember that the password is my friend.","a bicycle is my object.",
    "My colleague","Please tell me who Alex is.","Actually Alex is my dentist.",
])
def test_ambiguous_and_unsupported_reports_do_not_route_to_write(text):
    assert entity_report(text) is None
    assert decide_response(text).route!="ENTITY_REPORT"


def test_relation_phrase_is_not_a_standalone_route():
    assert entity_clarification_relation("My colleague.")=="COLLEAGUE"
    assert entity_clarification_relation("The friend")=="FRIEND"
    assert entity_clarification_relation("My colleague or friend") is None
    assert entity_clarification_relation('"My colleague"') is None
    assert decide_response("My colleague").route=="MODEL"


def _helpers():
    pytest.importorskip("psycopg")
    from test_mvp05c1_entity_continuity import (
        _helpers as h,_ask,_register,_amend,GuessingGateway,
    )
    actor,call,seed,turn=h()
    return actor,call,seed,turn,_ask,_register,_amend,GuessingGateway


def test_natural_reports_distinguish_registration_recognition_and_correction(dsn):
    import psycopg
    from persona_agent.entity_memory import find_entities
    from persona_agent.entity_conversation import REPORT_ROUTE_ID
    actor,call,seed,turn,ask,register,amend,Gateway=_helpers()
    user,instance,_=seed(dsn)
    g=Gateway()
    first,reply=ask(dsn,user,instance,"Alex is my friend.",g)
    assert reply.model_id==REPORT_ROUTE_ID and "Alex as your friend" in reply.text
    saved=call(dsn,find_entities,actor(user),instance,"Alex")[0]
    assert saved.relation_source_turn_id==first.turn_id and saved.version==1
    _,repeated=ask(dsn,user,instance,"Remember that Alex is my friend.",g)
    assert "Alex as your friend" in repeated.text
    assert call(dsn,find_entities,actor(user),instance,"Alex")[0].version==1
    _,conflict=ask(dsn,user,instance,"Alex is my colleague.",g)
    assert "correcting a saved relationship" in conflict.text
    assert call(dsn,find_entities,actor(user),instance,"Alex")[0].relation=="FRIEND"
    correction,corrected=ask(dsn,user,instance,"Actually, Alex is my colleague.",g)
    hit=call(dsn,find_entities,actor(user),instance,"Alex")[0]
    assert (hit.entity_id,hit.version,hit.relation,hit.relation_source_turn_id)==(
        saved.entity_id,2,"COLLEAGUE",correction.turn_id,
    )
    assert "Alex as your colleague" in corrected.text
    distinct,_=ask(dsn,user,instance,"Another Alex is my friend.",g)
    hits=call(dsn,find_entities,actor(user),instance,"Alex")
    assert len(hits)==2 and len({h.entity_id for h in hits})==2
    assert any(h.relation_source_turn_id==distinct.turn_id for h in hits)
    _,ambiguous=ask(dsn,user,instance,"Actually Alex is my sibling.",g)
    assert "more specifically" in ambiguous.text
    assert {h.relation for h in call(dsn,find_entities,actor(user),instance,"Alex")}=={
        "FRIEND","COLLEAGUE",
    }
    assert g.calls==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT action,version,source_turn_id FROM memory_entity_revisions
               WHERE entity_id=%s ORDER BY version""",(saved.entity_id,),
        ).fetchall()==[
            ("REGISTER",1,first.turn_id),("CORRECT_RELATION",2,correction.turn_id),
        ]


def test_forced_model_only_report_uses_original_input_not_model_claim(dsn):
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_,ask,_,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    g=Gateway()
    source,reply=ask(dsn,user,instance,"Remember that O’Neil is my colleague.",g,forced=True)
    assert g.calls==1 and reply.model_id=="guessing-entity-model"
    assert "O’Neil as your colleague" in reply.text and "dentist" not in reply.text
    hit=call(dsn,find_entities,actor(user),instance,"O'Neil")[0]
    assert hit.relation_source_turn_id==source.turn_id
    _,corrected=ask(dsn,user,instance,"Actually O'Neil is my friend.",g)
    assert "O’Neil as your friend" in corrected.text
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()


def test_report_replay_and_already_consumed_source_do_not_duplicate(dsn):
    from persona_agent.entity_memory import find_entities
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,seed,turn,ask,register,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    source,_,saved=register(dsn,user,instance,"Alex","FRIEND")
    g=Gateway()
    first=call(dsn,respond_to_user_text,actor(user),source.turn_id,g)
    again=call(dsn,respond_to_user_text,actor(user),source.turn_id,g)
    assert first.response_id==again.response_id and g.calls==0
    hits=call(dsn,find_entities,actor(user),instance,"Alex")
    assert [(h.entity_id,h.version) for h in hits]==[(saved.entity_id,1)]


def test_correction_without_identity_and_ineligible_name_do_not_create(dsn):
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_,ask,register,amend,Gateway=_helpers()
    user,instance,_=seed(dsn)
    g=Gateway()
    _,missing=ask(dsn,user,instance,"Actually Alex is my colleague.",g)
    assert "more specifically" in missing.text
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()
    _,_,saved=register(dsn,user,instance,"Alex","FRIEND")
    amend(dsn,user,instance,saved.entity_id,1,"Forget my saved entity Alex.","RETRACT")
    _,repeated=ask(dsn,user,instance,"Alex is my colleague.",g)
    assert "more specifically" in repeated.text
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()
    import psycopg
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_entities WHERE user_id=%s AND instance_id=%s",
            (user,instance),
        ).fetchone()[0]==1


def test_competing_first_reports_are_serialized_without_duplicate_identity(dsn):
    import psycopg
    from persona_agent.entity_memory import find_entities
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,seed,turn,_,_,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    sources=[turn(dsn,user,instance,uuid4(),"Alex is my friend.") for _ in range(2)]
    barrier=Barrier(2)
    def process(source):
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("SET lock_timeout='5s'")
            barrier.wait(timeout=5)
            return respond_to_user_text(conn,actor(user),source.turn_id,Gateway())
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(process,sources))
    assert all("Alex as your friend" in r.text for r in replies)
    hits=call(dsn,find_entities,actor(user),instance,"Alex")
    assert len(hits)==1 and hits[0].version==1


def test_report_and_response_roll_back_together(dsn,monkeypatch):
    import psycopg
    from persona_agent import entity_conversation
    from persona_agent.entity_memory import find_entities
    from persona_agent.conversation import lookup_response
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,seed,turn,_,_,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    source=turn(dsn,user,instance,uuid4(),"Alex is my friend.")
    real=entity_conversation.apply_entity_report
    def fail(*args):
        real(*args)
        raise RuntimeError("after owner write")
    monkeypatch.setattr(entity_conversation,"apply_entity_report",fail)
    with pytest.raises(RuntimeError,match="after owner write"):
        call(dsn,respond_to_user_text,actor(user),source.turn_id,Gateway())
    assert call(dsn,find_entities,actor(user),instance,"Alex")==()
    assert call(dsn,lookup_response,actor(user),source.turn_id) is None
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM command_receipts WHERE user_id=%s
               AND command_type='memory.entity.register.v1'""",(user,),
        ).fetchone()[0]==0
    monkeypatch.setattr(entity_conversation,"apply_entity_report",real)
    reply=call(dsn,respond_to_user_text,actor(user),source.turn_id,Gateway())
    assert "Alex as your friend" in reply.text


@pytest.mark.parametrize("forced",[False,True])
def test_delivered_choice_short_reply_survives_restart_and_binds_pronoun(dsn,forced):
    import psycopg
    from persona_agent.entity_memory import find_entities
    actor,call,seed,_,ask,register,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    _,_,friend=register(dsn,user,instance,"Alex","FRIEND")
    _,_,colleague=register(dsn,user,instance,"Alex","COLLEAGUE")
    conversation=uuid4()
    g=Gateway()
    _,prompt=ask(dsn,user,instance,"Who is Alex to me?",g,conversation=conversation)
    assert "friend Alex" in prompt.text and "colleague Alex" in prompt.text
    with psycopg.connect(dsn,autocommit=True) as conn:
        choices=conn.execute(
            """SELECT payload->'clarification_candidates' FROM events
               WHERE user_id=%s AND event_type='conversation.response.entity_grounded.v1'
                 AND payload->>'response_id'=%s""",(user,str(prompt.response_id)),
        ).fetchone()[0]
        assert {row[0] for row in choices}=={str(friend.entity_id),str(colleague.entity_id)}
        assert conn.execute(
            "SELECT count(*) FROM dialogue_pending_acts WHERE source_response_id=%s",
            (prompt.response_id,),
        ).fetchone()[0]==0
    _,selected=ask(dsn,user,instance,"My colleague.",g,conversation=conversation,forced=forced)
    assert "Alex as your colleague" in selected.text
    assert g.calls==(1 if forced else 0)
    _,again=ask(dsn,user,instance,"How do I know them?",g,conversation=conversation)
    assert again.text==selected.text
    assert {h.version for h in call(dsn,find_entities,actor(user),instance,"Alex")}=={1}


@pytest.mark.parametrize("change",["NEW_MATCH","ALIAS","RETRACT","RELATION"])
def test_choice_set_change_requires_fresh_identification(dsn,change):
    actor,call,seed,_,ask,register,amend,Gateway=_helpers()
    user,instance,_=seed(dsn)
    _,_,friend=register(dsn,user,instance,"Alex","FRIEND")
    _,_,colleague=register(dsn,user,instance,"Alex","COLLEAGUE")
    conversation=uuid4()
    g=Gateway()
    ask(dsn,user,instance,"Who is Alex to me?",g,conversation=conversation)
    if change=="NEW_MATCH":
        register(dsn,user,instance,"Alex","COLLEAGUE")
    elif change=="ALIAS":
        amend(dsn,user,instance,colleague.entity_id,1,
              "I also call Alex Al.","ADD_ALIAS",alias="Al")
    elif change=="RETRACT":
        amend(dsn,user,instance,friend.entity_id,1,"Forget my saved entity Alex.","RETRACT")
    else:
        amend(dsn,user,instance,colleague.entity_id,1,"Actually Alex is my sibling.",
              "CORRECT_RELATION",relation="SIBLING")
    _,reply=ask(dsn,user,instance,"My colleague",g,conversation=conversation)
    assert "saved matches changed" in reply.text and g.calls==0


def test_short_reply_without_delivered_adjacent_choice_keeps_model_path(dsn):
    from persona_agent.conversation import acknowledge_text_delivery
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,seed,turn,ask,register,_,Gateway=_helpers()
    user,instance,_=seed(dsn)
    register(dsn,user,instance,"Alex","FRIEND")
    register(dsn,user,instance,"Alex","COLLEAGUE")
    g=Gateway()
    conversation=uuid4()
    _,prompt=ask(dsn,user,instance,"Who is Alex to me?",g,
                 conversation=conversation,deliver=False)
    early=turn(dsn,user,instance,conversation,"My colleague")
    # Even a later ACK cannot turn an already-received message into a
    # response to a question not yet delivered at its receipt time.
    call(dsn,acknowledge_text_delivery,actor(user),prompt.response_id)
    response=call(dsn,respond_to_user_text,actor(user),early.turn_id,g)
    assert response.model_id=="guessing-entity-model"
    _,unrelated=ask(dsn,user,instance,"My colleague",g)
    assert unrelated.model_id=="guessing-entity-model"
    ask(dsn,user,instance,"Who is Alex to me?",g,conversation=conversation)
    ask(dsn,user,instance,"Explain TCP sockets.",g,conversation=conversation)
    _,intervening=ask(dsn,user,instance,"My colleague",g,conversation=conversation)
    assert intervening.model_id=="guessing-entity-model"


def test_same_relation_or_wrong_relation_does_not_select_a_candidate(dsn):
    actor,call,seed,_,ask,register,amend,Gateway=_helpers()
    user,instance,_=seed(dsn)
    register(dsn,user,instance,"Alex","FRIEND")
    _,_,sam=register(dsn,user,instance,"Sam","FRIEND")
    amend(dsn,user,instance,sam.entity_id,1,"I also call Sam Alex.","ADD_ALIAS",alias="Alex")
    g=Gateway()
    conv=uuid4()
    ask(dsn,user,instance,"Who is Alex to me?",g,conversation=conv)
    _,reply=ask(dsn,user,instance,"My friend",g,conversation=conv)
    assert "does not identify one" in reply.text
    conv=uuid4()
    ask(dsn,user,instance,"Who is Alex to me?",g,conversation=conv)
    _,wrong=ask(dsn,user,instance,"My sibling",g,conversation=conv)
    assert "does not identify one" in wrong.text
    assert g.calls==0
