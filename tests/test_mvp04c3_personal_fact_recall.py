"""MVP-04C.3 exact-slot recall, source semantics and fenced delivery."""
from uuid import uuid4

import pytest

from persona_agent.personal_fact_recall import (
    ROUTE_ID,render_saved_fact,source_supports_value,
)
from persona_agent.response_decisions import decide_response,personal_fact_topic


@pytest.mark.parametrize("question,topic",[
    ("By the way, what car do I drive?","car"),
    ("Which phone do I use?","phone"),
    ("What camera do I own?","camera"),
])
def test_exact_fact_relations_route(question,topic):
    assert personal_fact_topic(question)==topic
    assert decide_response(question).route=="PERSONAL_FACT_RECALL"


@pytest.mark.parametrize("question",[
    "What command do I use?",
    "Which tool do I use?",
    "What camera should I buy?",
    "What car do I own?",
    "What camera do I use?",
    "What phone number do I use?",
    "What if I asked what car do I drive?",
    '"What car do I drive?"',
    "What car do I drive? And where should I go?",
])
def test_advice_different_relations_and_mixed_requests_stay_model_driven(question):
    assert personal_fact_topic(question) is None
    assert decide_response(question).route=="MODEL"


@pytest.mark.parametrize("topic,value,source,expected",[
    ("car","Toyota Camry","I drive a Toyota Camry.",True),
    ("phone","Fairphone","I use a Fairphone.",True),
    ("camera","Canon EOS R5","Actually, I now own a Canon EOS R5.",True),
    ("car","Toyota Camry","I own a Toyota Camry.",False),
    ("car","Toyota Camry","I used to drive a Toyota Camry.",False),
    ("car","Toyota Camry","I don't drive a Toyota Camry.",False),
    ("car","Toyota Camry","Maybe I drive a Toyota Camry.",False),
    ("car","Toyota Camry","I drive a Toyota Camry only on weekends.",False),
    ("car","Toyota Camry",'"I drive a Toyota Camry."',False),
    ("car","Toyota Camry","I drive a Toyota Camry?",False),
    ("car","Toyota Camry","I drive a Honda Civic.",False),
    ("phone","Fairphone","My friend said I use a Fairphone.",False),
    ("phone","password","I use a password.",False),
])
def test_full_source_must_support_value_and_relation(topic,value,source,expected):
    assert source_supports_value(topic,value,source,source) is expected


def test_quoted_substring_cannot_override_qualification():
    assert not source_supports_value(
        "car","Toyota Camry","I drive a Toyota Camry only on weekends.",
        "I drive a Toyota Camry",
    )


def row(**changes):
    record=dict(eligible=True,disputed=False,value_text="Toyota Camry",
                original_source_text="I drive a Toyota Camry.",
                source_quote="I drive a Toyota Camry.",
                memory_id=uuid4(),version=1,source_turn_id=uuid4())
    record.update(changes)
    return record


@pytest.mark.parametrize("record,status",[
    (None,"UNKNOWN"),
    (row(eligible=False),"UNKNOWN"),
    (row(disputed=True),"DISPUTED"),
    (row(original_source_text="I used to drive a Toyota Camry."),"UNSUPPORTED_SOURCE"),
])
def test_abstention_has_no_private_value_or_evidence_reference(record,status):
    answer=render_saved_fact("car",record)
    assert answer.status==status
    assert "Toyota" not in answer.text
    assert answer.memory_id is None and answer.source_turn_id is None


def test_known_answer_carries_current_revision_and_user_report_attribution():
    record=row(version=3)
    answer=render_saved_fact("car",record)
    assert answer.status=="KNOWN" and "last saved report" in answer.text
    assert "Toyota Camry" in answer.text
    assert answer.memory_id==record["memory_id"]
    assert answer.version==3 and answer.source_turn_id==record["source_turn_id"]


@pytest.fixture
def memory_setup(dsn):
    pytest.importorskip("psycopg")
    from test_phase2_memory import seed,turn,call,actor,candidate
    from persona_agent.memory import admit_memory
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,"I drive a Toyota Camry.")
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,conversation,source,kind="FACT",predicate="car",
        value="Toyota Camry",quote="I drive a Toyota Camry.",
    ))
    return user,instance,conversation,source,saved


class GuessingModel:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        self.calls+=1
        return ModelReply(model_id="guessing-model",text="You drive a blue Bentley.")


def test_exact_recall_auto_and_model_paths_are_source_backed(dsn,memory_setup):
    from test_phase2_memory import seed,turn,call,actor
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import respond_to_text,acknowledge_text_delivery
    user,instance,_,source,saved=memory_setup
    question="What car do I drive?"
    model=GuessingModel()
    current=turn(dsn,user,instance,uuid4(),question)
    auto=call(dsn,respond_to_user_text,actor(user),current.turn_id,model)
    assert auto.model_id==ROUTE_ID and "Toyota Camry" in auto.text
    assert model.calls==0
    call(dsn,acknowledge_text_delivery,actor(user),auto.response_id)
    replay=call(dsn,respond_to_user_text,actor(user),current.turn_id,model)
    assert replay.response_id==auto.response_id and replay.text==auto.text
    assert model.calls==0
    current=turn(dsn,user,instance,uuid4(),question)
    forced=call(dsn,respond_to_text,actor(user),current.turn_id,model)
    assert forced.model_id=="guessing-model" and forced.text==auto.text
    assert model.calls==1
    outsider,other,_=seed(dsn)
    missing=turn(dsn,outsider,other,uuid4(),question)
    unknown=call(dsn,respond_to_text,actor(outsider),missing.turn_id,model)
    assert unknown.text=="I don't have a reliable saved answer for that."
    import psycopg
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT payload->>'status',payload->>'memory_id',
                      payload->>'memory_version',payload->>'evidence_turn_id',
                      payload->>'model_reply_replaced'
               FROM events WHERE user_id=%s
                 AND event_type='conversation.response.personal_fact_grounded.v1'
                 AND payload->>'response_id'=%s""",
            (user,str(forced.response_id)),
        ).fetchone()==("KNOWN",str(saved.memory_id),"1",str(source.turn_id),"true")


def test_owner_correction_and_retraction_change_later_answers(dsn,memory_setup):
    from test_phase2_memory import turn,call,actor,correction
    from persona_agent.memory import amend_memory
    from persona_agent.response_orchestrator import respond_to_user_text
    user,instance,conv,_,saved=memory_setup
    model=GuessingModel()
    new=turn(dsn,user,instance,conv,"Actually, I now drive a Honda Civic.")
    corrected=call(dsn,amend_memory,actor(user),correction(
        instance,conv,new,saved.memory_id,value="Honda Civic",
        quote="Actually, I now drive a Honda Civic.",
    ))
    question=turn(dsn,user,instance,uuid4(),"What car do I drive?")
    answer=call(dsn,respond_to_user_text,actor(user),question.turn_id,model)
    assert corrected.version==2 and "Honda Civic" in answer.text
    assert "Toyota" not in answer.text
    retract=turn(dsn,user,instance,conv,"I retract the saved car report.")
    call(dsn,amend_memory,actor(user),correction(
        instance,conv,retract,saved.memory_id,version=2,
        quote="I retract the saved car report.",action="RETRACT",
    ))
    question=turn(dsn,user,instance,uuid4(),"What car do I drive?")
    answer=call(dsn,respond_to_user_text,actor(user),question.turn_id,model)
    assert answer.text=="I don't have a reliable saved answer for that."
    assert model.calls==0
