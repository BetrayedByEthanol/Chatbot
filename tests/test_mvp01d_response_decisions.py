"""MVP-01D: decision precision, authenticated routing and tool continuation."""

from uuid import UUID

import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import LocalAcceptanceSession, seed_local_demo
from persona_agent.response_decisions import (
    decide_response, explicit_analysis_payload,
)
from persona_agent.tool_contracts import SLOW_TOOL, TIME_TOOL
from persona_agent.tool_gateway import run_one_text_tool
from persona_agent.conversation_contracts import ModelReply


class RecordingModel:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="mvp01d-model",text="Let's talk about that.")


class NoInference:
    def generate(self,messages):
        raise AssertionError("high precision tool routing must avoid inference")


@pytest.mark.parametrize("message,zone",[
    ("What time is it?","UTC"),
    ("What time is it in Berlin?","Europe/Berlin"),
    ("What's the current time in Tokyo?","Asia/Tokyo"),
    ("Current time in London","Europe/London"),
    ("What is the time for America/New_York?","America/New_York"),
])
def test_clock_decision_selects_only_a_known_timezone(message,zone):
    decision=decide_response(message)
    assert decision.route=="CLOCK"
    assert decision.timezone_name==zone


@pytest.mark.parametrize("message",[
    "What time is it on Mars?",
    "What time is it tomorrow?",
    "What is time complexity?",
    "What if the time in Berlin were different?",
    "Do you have time to help?",
    "How do I create a selfsigned certificate?",
    "Analyze this text for me",
    "Count the words, please.",
    '"What time is it in Berlin?"',
    "Please remember that I prefer Berlin.",
])
def test_ambiguous_non_tool_messages_abstain(message):
    assert decide_response(message).route=="MODEL"


@pytest.mark.parametrize("message,payload",[
    ("Analyze this text: alpha beta beta","alpha beta beta"),
    ("Count words in: one two three","one two three"),
    ("Word count for: hello world","hello world"),
    ("Please analyze this text:\nblue red red","blue red red"),
])
def test_analysis_is_delimited_and_uses_only_user_supplied_payload(message,payload):
    assert decide_response(message).route=="TEXT_ANALYSIS"
    assert explicit_analysis_payload(message)==payload


def test_natural_clock_route_uses_authenticated_response_contract(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    host=LocalAcceptanceSession(dsn,identity,NoInference(),output.append)
    first=host.message(
        text="What time is it in Berlin?",idempotency_key="clock-route",
    )
    assert first["model_id"]==TIME_TOOL
    assert first["text"].startswith("Current time in Europe/Berlin:")
    assert len(output)==1
    restored=LocalAcceptanceSession(dsn,identity,NoInference(),output.append)
    replay=restored.message(
        text="What time is it in Berlin?",idempotency_key="clock-route",
    )
    assert replay["response_id"]==first["response_id"]
    assert replay["replayed"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_tool_calls
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s
               AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT delivery_status,model_id FROM conversation_responses
               WHERE turn_id=%s AND user_id=%s""",
            (UUID(first["turn_id"]),identity.user_id),
        ).fetchone()==("DELIVERED",TIME_TOOL)


def test_natural_analysis_routes_to_durable_async_worker_and_followup(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    host=LocalAcceptanceSession(
        dsn,identity,NoInference(),output.append,await_seconds=0,
    )
    first=host.message(
        text="Count words in: alpha beta beta gamma",
        idempotency_key="natural-analyze",max_words=2,
    )
    assert first["model_id"]=="tool.initial.deferred.v1"
    assert "follow up" in first["text"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        recorded=conn.execute(
            """SELECT tool_config,admission_state FROM conversation_tool_calls
               WHERE turn_id=%s AND user_id=%s""",
            (UUID(first["turn_id"]),identity.user_id),
        ).fetchone()
        assert recorded==({"max_words":2},"QUEUED")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert run_one_text_tool(conn,worker_id="mvp01d-test-worker") is not None
    assert host.poll_followups()==1
    assert output[-1]["type"]=="followup"
    assert "4 words, 3 unique" in output[-1]["text"]
    assert "beta (2)" in output[-1]["text"]
    assert "count (1)" not in output[-1]["text"]
    assert host.poll_followups()==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_assistant_followups
               WHERE tool_call_id=%s""",
            (UUID(output[-1]["tool_call_id"]),),
        ).fetchone()[0]==1


def test_unrecognized_input_falls_back_to_one_model_response(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    model=RecordingModel()
    host=LocalAcceptanceSession(dsn,identity,model,output.append)
    first=host.message(
        text="What time is it tomorrow?",idempotency_key="ambiguous",
    )
    assert first["model_id"]=="mvp01d-model"
    assert model.calls==1
    next_turn=host.message(
        text="I want to learn Python.",idempotency_key="goal-signal",
    )
    assert next_turn["model_id"]=="mvp01d-model"
    assert model.calls==2
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_tool_calls
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0


def test_trusted_operator_can_still_override_model_and_tool_modes(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    model=RecordingModel()
    host=LocalAcceptanceSession(dsn,identity,model,output.append)
    first=host.message(
        text="What time is it in Tokyo?",
        idempotency_key="model-override",mode="model",
    )
    assert first["model_id"]=="mvp01d-model"
    assert model.calls==1
    second=host.message(
        text="Please discuss schedules",
        idempotency_key="explicit-time",mode="time",
        timezone_name="Europe/Berlin",
    )
    assert second["model_id"]==TIME_TOOL
    with pytest.raises(ValueError,match="unknown message mode"):
        host.message(text="hello",idempotency_key="bad-mode",mode="delete")


def test_unsafe_model_text_cannot_trigger_tool_execution(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    class Model:
        def generate(self,messages):
            return ModelReply(
                model_id="mvp01d-model",
                text="Count words in: alpha beta beta",
            )
    host=LocalAcceptanceSession(dsn,identity,Model(),output.append)
    response=host.message(text="Hello",idempotency_key="plain")
    assert response["text"]=="Count words in: alpha beta beta"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_tool_calls
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==0
