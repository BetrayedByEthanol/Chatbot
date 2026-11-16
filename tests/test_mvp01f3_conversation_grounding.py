"""MVP-01F.3 grounded recollection/summary and safe cross-session topic shifts."""
import pytest
pytest.importorskip("psycopg")
import psycopg

from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.acceptance_host import seed_local_demo
from persona_agent.conversation_contracts import ModelReply
from persona_agent.conversation_grounding import (
    grounding_context_block, grounding_mode,
)
from persona_agent.contracts import TrustedActor
from persona_agent.dialogue_state import load_active_dialogue
from persona_agent.intent_interpretation import explicit_unresolved_named_topic


class CapturingModel:
    def __init__(self):
        self.calls=[]
    def generate(self,messages):
        self.calls.append(tuple(messages))
        if "Do you remember" in messages[-1].content:
            # Simulate a prior ungrounded assistant claim. It must not
            # become an attributed fact in the *subsequent* summary prompt.
            return ModelReply(model_id="f3-model",
                              text="We discussed walking posture and stride.")
        return ModelReply(model_id="f3-model",text="We can discuss it.")
    def generate_interpreted(self,messages):
        self.calls.append(tuple(messages))
        return ModelReply(model_id="f3-model",text="We can discuss it.")


def test_grounding_view_quotes_only_first_party_input_and_preserves_time():
    assert grounding_mode("Could you summarize our discussion?")=="SUMMARY"
    assert grounding_mode("Do you remember what we discussed about walking?")=="RECALL"
    assert grounding_mode("Let's discuss walking.") is None
    assert explicit_unresolved_named_topic(
        "Let's discuss my walking plan instead."
    )=="walking plan"
    assert explicit_unresolved_named_topic(
        "What about that walking goal?"
    ) is None
    assert explicit_unresolved_named_topic(
        "What if we discussed my walking goal?"
    ) is None
    recall=grounding_context_block(
        "Do you remember what we discussed about walking?",
        ["Hi, I'm back."],max_chars=900,
    )
    assert "none matching" in recall
    assert "cannot reliably recall" in recall
    facts=grounding_context_block(
        "Could you summarize our discussion and suggest one next step?",
        [
            "I managed to walk yesterday.",
            "It rained today, so I skipped my walk.",
        ],max_chars=1000,
    )
    assert "I managed to walk yesterday." in facts
    assert "It rained today, so I skipped my walk." in facts
    assert "assistant messages are NOT independent evidence" in facts
    assert len(facts)<=1000
    assert grounding_context_block(
        "Could you summarize our discussion?",["I walked yesterday."],
        max_chars=150,
    )==""
    assert grounding_context_block(
        "How do I install Nginx?",["I walked yesterday."],
        max_chars=1000,
    )==""


def test_fresh_cross_session_named_switch_creates_discussion_not_false_recall(dsn):
    model=CapturingModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("walking")
    host.send("I want to walk more regularly.",idempotency_key="oldwalk")
    host.begin_session("nginx")
    host.send("Can you help me set up Nginx?",idempotency_key="nginx")
    prev=host.active_identity
    with psycopg.connect(dsn,autocommit=True) as conn:
        actor=TrustedActor(user_id=prev.user_id,source_id="f3-before")
        before=load_active_dialogue(conn,actor,prev.conversation_id)
        nginx=next(t.title for t in before.threads
                   if t.thread_id==before.focus_thread_id)
        assert "Nginx" in nginx

    host.send(
        "Let's discuss my walking plan instead.",
        idempotency_key="newwalking",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        after=load_active_dialogue(
            conn,TrustedActor(user_id=prev.user_id,source_id="f3-after"),
            prev.conversation_id,
        )
        assert len(after.threads)==2
        title=next(t.title for t in after.threads
                   if t.thread_id==after.focus_thread_id)
        assert title=="walking plan"
        assert all(t.title!="I want to walk more regularly." for t in after.threads)
        assert conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s ORDER BY turn_sequence DESC LIMIT 1""",
            (prev.conversation_id,),
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s",
            (prev.user_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",
            (prev.user_id,),
        ).fetchone()[0]==0


def test_recall_and_summary_prompt_uses_user_evidence_not_model_fabrications(dsn):
    model=CapturingModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("historic")
    host.send("I want to walk twice per week.",idempotency_key="first")
    host.begin_session("return")
    host.send("Hi, I'm back.",idempotency_key="hello")
    host.send(
        "Do you remember what we discussed about walking?",
        idempotency_key="recall",
    )
    recall_prompt=model.calls[-1][0].content
    assert "Conversation grounding" in recall_prompt
    assert "none matching the requested earlier discussion" in recall_prompt
    assert "cannot reliably recall" in recall_prompt
    assert "I want to walk twice per week." not in recall_prompt

    host.send("I managed to walk yesterday.",idempotency_key="success")
    host.send("It rained today, so I skipped my walk.",idempotency_key="rain")
    host.restart_host()
    host.send(
        "Could you summarize our discussion and suggest one next step?",
        idempotency_key="summary",
    )
    prompt=model.calls[-1][0].content
    assert "Conversation grounding" in prompt
    assert "I managed to walk yesterday." in prompt
    assert "It rained today, so I skipped my walk." in prompt
    assert "Prior assistant messages are NOT independent evidence" in prompt
    quotes=prompt.split("User statements in accessible current conversation:",1)[1]
    assert "We discussed walking posture and stride." not in quotes.split("\n",1)[0]
    with psycopg.connect(dsn,autocommit=True) as conn:
        identity=host.active_identity
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s",
            (identity.user_id,),
        ).fetchone()[0]==0
