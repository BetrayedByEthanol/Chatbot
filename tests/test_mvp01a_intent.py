"""MVP-01A behavioral and PostgreSQL acceptance of tentative intent signals."""
from uuid import UUID, uuid4

import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation import (
    ingest_text, prepare_text_context, respond_to_text,
)
from persona_agent.conversation_contracts import (
    ModelReply, TextInputCommand,
)
from persona_agent.contracts import TrustedActor
from persona_agent.intent_interpretation import classify_intent
from persona_agent.runtime import PermissionDenied


class RecordingModel:
    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(messages)
        return ModelReply(
            model_id="mvp01a-recording-model",
            text="I can help clarify that objective.",
        )


@pytest.mark.parametrize("message,kind", [
    ("I want to eat healthier.", "OBJECTIVE"),
    ("I'd like to exercise more.", "OBJECTIVE"),
    ("My goal is to learn Python.", "OBJECTIVE"),
    ("I want to know what time it is.", "REQUEST"),
    ("Can we plan the next step?", "REQUEST"),
    ("Yes, please help me with the goal.", "REQUEST"),
    ("Please remember that I prefer short answers.", "INSTRUCTION"),
    ("Don't send email.", "INSTRUCTION"),
    ("Actually, I meant that goal for next week.", "CORRECTION"),
    ("I prepared a healthy lunch.", "OBSERVATION"),
    ("What if I said I want to run daily?", "HYPOTHETICAL"),
    ("For example, I want to eat healthier.", "HYPOTHETICAL"),
    ("Hello.", None),
    ("okay", None),
    ("Maybe a different approach.", None),
])
def test_bounded_speech_act_classifier(message, kind):
    result = classify_intent(message)
    assert (result.kind if result else None) == kind
    if result:
        assert result.method_revision == "mvp01a.rules.1"
        assert 0 < result.confidence <= 1
        assert len(result.label) <= 200


def test_multi_turn_goal_discussion_is_not_approval_or_completion(dsn):
    identity = seed_local_demo(dsn)
    model = RecordingModel()
    harness = BehavioralAcceptanceHarness(dsn, identity, model)
    harness.begin_session("goal")

    first = harness.send(
        "I want to eat healthier.",
        idempotency_key="mvp01a-objective",
    )
    second = harness.send(
        "Could we plan the next step?",
        idempotency_key="mvp01a-referent",
    )
    third = harness.send(
        "Yes, please help me with the goal.",
        idempotency_key="mvp01a-ack",
    )
    assert first.turn_id != second.turn_id != third.turn_id
    assert len(model.calls) == 3  # no independent interpretation LLM
    assert all("Tentative user-intent cues" in call[0].content for call in model.calls)
    assert "OBJECTIVE" in model.calls[0][0].content
    assert "eat healthier" in model.calls[1][0].content
    assert "approved goals" in model.calls[1][0].content
    harness.restart_host()
    samples=harness.observe_intent_signals()
    assert [row["intent_kind"] for row in samples] == [
        "OBJECTIVE","REQUEST","REQUEST",
    ]
    assert all(row["proposal_status"]=="UNREVIEWED" for row in samples)

    with psycopg.connect(dsn, autocommit=True) as conn:
        signals = conn.execute(
            """SELECT intent_kind,related_signal_id,turn_id,
                      epistemic_status,proposal_status
               FROM conversation_intent_signals
               WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
               ORDER BY turn_sequence""",
            (identity.conversation_id, identity.user_id, identity.instance_id),
        ).fetchall()
        objective_id = conn.execute(
            """SELECT signal_id FROM conversation_intent_signals
               WHERE turn_id=%s""", (UUID(first.turn_id),),
        ).fetchone()[0]
        assert [v[0] for v in signals] == [
            "OBJECTIVE", "REQUEST", "REQUEST",
        ]
        assert signals[1][1] == objective_id
        assert signals[2][1] == objective_id
        assert all(v[3:] == ("RULE_INFERRED", "UNREVIEWED") for v in signals)
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (identity.user_id, identity.instance_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.intent.signal_recorded.v1'""",
            (identity.user_id, identity.instance_id),
        ).fetchone()[0] == 3


def test_correction_is_linked_but_never_overwrites_prior_goal(dsn):
    identity=seed_local_demo(dsn)
    model=RecordingModel()
    host=BehavioralAcceptanceHarness(dsn, identity, model)
    host.begin_session("correction")
    host.send("My goal is to cook more.", idempotency_key="objective")
    host.send(
        "Actually, I meant that goal for next year.",
        idempotency_key="correction",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT signal_id,intent_kind,label,related_signal_id
               FROM conversation_intent_signals
               WHERE conversation_id=%s ORDER BY turn_sequence""",
            (identity.conversation_id,),
        ).fetchall()
    assert len(rows)==2
    assert rows[0][1:] == ("OBJECTIVE","cook more",None)
    assert rows[1][1]=="CORRECTION"
    assert rows[1][3]==rows[0][0]
    assert model.calls[1][0].content.find("cook more")>=0


def test_ambiguous_objective_reference_abstains(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,RecordingModel())
    host.begin_session("ambiguous")
    host.send("I want to walk more.",idempotency_key="one")
    host.send("I want to cook more.",idempotency_key="two")
    host.send(
        "Actually, I meant that goal for next year.",
        idempotency_key="three",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        related=conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s AND intent_kind='CORRECTION'""",
            (identity.conversation_id,),
        ).fetchone()
    assert related==(None,)


def test_hypothetical_and_unsupported_text_never_create_domain_state(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,RecordingModel())
    host.begin_session("hypothetical")
    host.send(
        "What if I said I want to spend money?",
        idempotency_key="hypothetical",
    )
    host.send("okay", idempotency_key="empty")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT intent_kind FROM conversation_intent_signals
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchall()==[("HYPOTHETICAL",)]
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0


def test_replay_and_tenant_scope_preserve_evidence(dsn):
    first=seed_local_demo(dsn)
    second=seed_local_demo(dsn)
    model=RecordingModel()
    host=BehavioralAcceptanceHarness(dsn,first,model)
    host.begin_session("first")
    a=host.send("I want to read more.",idempotency_key="stable")
    host.restart_host()
    b=host.send("I want to read more.",idempotency_key="stable")
    assert b.turn_id==a.turn_id
    assert len(model.calls)==1

    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_intent_signals
               WHERE user_id=%s AND instance_id=%s""",
            (first.user_id,first.instance_id),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM conversation_intent_signals
               WHERE user_id=%s AND instance_id=%s""",
            (second.user_id,second.instance_id),
        ).fetchone()[0]==0
        with pytest.raises(PermissionDenied):
            prepare_text_context(
                conn, TrustedActor(user_id=second.user_id,source_id="scope-test"),
                UUID(a.turn_id),
            )


def test_short_prompt_budget_preserves_latest_input(dsn):
    identity=seed_local_demo(dsn)
    actor=TrustedActor(user_id=identity.user_id,source_id="mvp01a-budget")
    with psycopg.connect(dsn,autocommit=True) as conn:
        turn=ingest_text(
            conn,actor,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                idempotency_key="small-budget",
                text="I want to build a vegetable garden.",
            ),
        )
        context=prepare_text_context(
            conn,actor,turn.turn_id,max_context_chars=512,
        )
        assert context.messages[-1].content=="I want to build a vegetable garden."
        assert len(context.messages[0].content)<512
