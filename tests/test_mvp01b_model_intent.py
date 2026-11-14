"""MVP-01B: real ingress, single-pass model proposals and stale-reply fencing."""
from uuid import UUID, uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import (
    ModelIntentCandidate, ModelReply, StaleConversation, TextInputCommand,
)
from persona_agent.contracts import TrustedActor
from persona_agent.intent_interpretation import should_interpret_with_model


class EnrichedModel:
    def __init__(self, candidate=None, during=None):
        self.candidate = candidate
        self.during = during
        self.plain_calls = 0
        self.enriched_calls = 0

    def generate(self, messages):
        self.plain_calls += 1
        return ModelReply(model_id="mvp01b-fake",text="Normal answer.")

    def generate_interpreted(self, messages):
        self.enriched_calls += 1
        if self.during:
            self.during()
        return ModelReply(
            model_id="mvp01b-fake",
            text="I hear you. What would make this practical?",
            intent_candidate=self.candidate,
        )


def candidate(kind="OBJECTIVE", quote="Lately I've been thinking about walking more.",
              confidence=0.9, reference="NONE"):
    return ModelIntentCandidate(
        kind=kind,evidence_quote=quote,
        confidence=confidence,reference=reference,
    )


@pytest.mark.parametrize("message,expected", [
    ("Lately I've been thinking about walking more.",True),
    ("Maybe I should reduce sweets.",True),
    ("I wish exercise felt easier.",True),
    ("That sounds manageable.",True),
    ("I want to exercise more.",False),
    ("What if I wanted to exercise more?",False),
    ("Please remember my preference.",False),
    ("Hello",False),
    ("okay",False),
    ("My favorite drink is tea.",False),
])
def test_selective_primary_model_route(message,expected):
    assert should_interpret_with_model(message)==expected


def test_model_proposal_is_evidence_linked_and_no_goal_is_authorized(dsn):
    identity=seed_local_demo(dsn)
    msg="Lately I've been thinking about walking more."
    gateway=EnrichedModel(candidate(quote=msg))
    harness=BehavioralAcceptanceHarness(dsn,identity,gateway)
    harness.begin_session("implicit")
    answer=harness.send(msg,idempotency_key="mvp01b-first")
    assert "I hear you" in answer.text
    assert gateway.enriched_calls==1 and gateway.plain_calls==0

    recorded=harness.observe_intent_signals()
    assert len(recorded)==1
    signal=recorded[0]
    assert signal["intent_kind"]=="OBJECTIVE"
    assert signal["epistemic_status"]=="MODEL_PROPOSED"
    assert signal["proposal_status"]=="UNREVIEWED"
    assert signal["label"]==msg
    assert signal["method_revision"]=="mvp01b.primary.1"
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT s.source_event_id,s.source_response_id,
                      t.source_input_event_id
               FROM conversation_intent_signals s
               JOIN conversation_turns t ON t.turn_id=s.turn_id
               WHERE s.turn_id=%s""",
            (UUID(answer.turn_id),),
        ).fetchone()
        assert row[0]==row[2] and str(row[1])==answer.response_id
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.intent.model_signal_recorded.v1'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==1


def test_followup_reference_links_only_one_prior_objective(dsn):
    identity=seed_local_demo(dsn)
    message="That sounds manageable."
    gateway=EnrichedModel(candidate(
        kind="REQUEST",quote=message,reference="PRIOR_OBJECTIVE",
    ))
    host=BehavioralAcceptanceHarness(dsn,identity,gateway)
    host.begin_session("link")
    host.send("I want to eat healthier.",idempotency_key="goal")
    host.send(message,idempotency_key="reference")
    assert gateway.plain_calls==1 and gateway.enriched_calls==1
    rows=host.observe_intent_signals()
    assert len(rows)==2
    with psycopg.connect(dsn,autocommit=True) as conn:
        obj=conn.execute(
            """SELECT signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s AND intent_kind='OBJECTIVE'""",
            (identity.conversation_id,),
        ).fetchone()[0]
    assert rows[1]["related_signal_id"]==str(obj)
    assert rows[1]["intent_kind"]=="REQUEST"
    assert rows[1]["epistemic_status"]=="MODEL_PROPOSED"


def test_ambiguous_model_reference_is_not_linked(dsn):
    identity=seed_local_demo(dsn)
    message="That sounds manageable."
    gateway=EnrichedModel(candidate(
        kind="REQUEST",quote=message,reference="PRIOR_OBJECTIVE",
    ))
    host=BehavioralAcceptanceHarness(dsn,identity,gateway)
    host.begin_session("ambiguity")
    host.send("I want to walk more.",idempotency_key="one")
    host.send("I want to cook more.",idempotency_key="two")
    host.send(message,idempotency_key="three")
    assert gateway.enriched_calls==1
    rows=host.observe_intent_signals()
    assert rows[-1]["related_signal_id"] is None


@pytest.mark.parametrize("proposed", [
    candidate(quote="invented objective that the user never mentioned"),
    candidate(confidence=0.2),
    candidate(kind="INSTRUCTION"),
    {"kind":"OBJECTIVE","evidence_quote":"Lately","confidence":1.0},
])
def test_untrusted_or_invalid_proposal_is_ignored_but_reply_delivered(dsn,proposed):
    identity=seed_local_demo(dsn)
    msg="Lately I've been thinking about walking more."
    gateway=EnrichedModel(proposed)
    host=BehavioralAcceptanceHarness(dsn,identity,gateway)
    host.begin_session("discard")
    response=host.send(msg,idempotency_key="discard-proposal")
    assert response.text.startswith("I hear you.")
    assert gateway.enriched_calls==1
    assert host.observe_intent_signals()==[]


def test_stale_model_reply_cannot_commit_intent_or_response(dsn):
    identity=seed_local_demo(dsn)
    message="Lately I've been thinking about walking more."
    auth=TrustedActor(
        user_id=identity.user_id,
        source_id="mvp01b-concurrent-ingress",
    )

    def supersede():
        with psycopg.connect(dsn,autocommit=True) as conn:
            ingest_text(
                conn,auth,TextInputCommand(
                    conversation_id=identity.conversation_id,
                    instance_id=identity.instance_id,
                    expected_generation=identity.generation,
                    idempotency_key="supersede",
                    text="Another message before model completes.",
                ),
            )

    model=EnrichedModel(candidate(quote=message),during=supersede)
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("stale")
    with pytest.raises(StaleConversation):
        host.send(message,idempotency_key="slow-original")
    assert model.enriched_calls==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM conversation_intent_signals
               WHERE conversation_id=%s AND user_id=%s""",
            (identity.conversation_id,identity.user_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM conversation_responses
               WHERE conversation_id=%s AND user_id=%s""",
            (identity.conversation_id,identity.user_id),
        ).fetchone()[0]==0


def test_retry_after_restart_does_not_duplicate_model_cue(dsn):
    identity=seed_local_demo(dsn)
    message="Lately I've been thinking about walking more."
    model=EnrichedModel(candidate(quote=message))
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("idempotent")
    one=host.send(message,idempotency_key="same")
    host.restart_host()
    two=host.send(message,idempotency_key="same")
    assert one.turn_id==two.turn_id
    assert model.enriched_calls==1
    assert len(host.observe_intent_signals())==1
