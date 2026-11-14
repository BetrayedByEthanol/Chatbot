"""MVP-01C real PostgreSQL tests: automatic dialogue, never goal progress."""
from uuid import UUID, uuid4

import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, respond_to_text,
)
from persona_agent.conversation_contracts import (
    ModelIntentCandidate, ModelReply, TextInputCommand,
)
from persona_agent.dialogue_contracts import Attention, Resolution
from persona_agent.dialogue_state import load_active_dialogue


class ReplyModel:
    def __init__(self, replies):
        self.replies=iter(replies)
        self.calls=0

    def generate(self, messages):
        self.calls+=1
        return ModelReply(model_id="dialogue-mvp01c",text=next(self.replies))


def state(dsn,identity):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return load_active_dialogue(
            conn,TrustedActor(user_id=identity.user_id,source_id="mvp01c-test"),
            identity.conversation_id,
        )


def test_objective_creates_focused_topic_and_delivered_question(dsn):
    identity=seed_local_demo(dsn)
    model=ReplyModel((
        "We could start small. Which day would work for you?",
        "Tuesday is a good start.",
    ))
    harness=BehavioralAcceptanceHarness(dsn,identity,model)
    harness.begin_session("goal-dialogue")
    first=harness.send(
        "I want to walk more.",idempotency_key="goal",
    )
    active=state(dsn,identity)
    assert len(active.threads)==1
    thread=active.threads[0]
    assert thread.title=="walk more"
    assert thread.attention_status==Attention.FOCUSED
    assert thread.resolution_status==Resolution.OPEN
    assert active.focus_thread_id==thread.thread_id
    assert len(active.pending_acts)==1
    assert active.pending_acts[0].kind=="QUESTION"
    assert active.pending_acts[0].label=="Which day would work for you?"
    assert len(active.referents)==1
    assert active.referents[0].label=="walk more"
    assert thread.next_conversational_action.startswith("Await answer:")

    harness.restart_host()
    harness.send("Tuesday would work.",idempotency_key="answered")
    after=state(dsn,identity)
    assert after.focus_thread_id==thread.thread_id
    assert after.pending_acts==()
    assert after.threads[0].next_conversational_action is None
    assert after.threads[0].resolution_status==Resolution.OPEN
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind,status,resolution_kind
               FROM dialogue_pending_acts WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==(
            "AUTO_DELIVERED_QUESTION","RESOLVED","ANSWERED",
        )
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM operations WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==2
    assert model.calls==2
    assert first.turn_id


def test_topic_switch_preserves_independent_resolution_and_old_question(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "What's stopping you from walking?",
        "We can discuss recipes.",
    )))
    harness.begin_session("topics")
    harness.send("I want to walk more.",idempotency_key="walk")
    walk=state(dsn,identity).focus_thread_id
    harness.send(
        "Let's discuss cooking instead.",idempotency_key="cooking",
    )
    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id!=walk
    status={t.thread_id:t for t in active.threads}
    assert status[walk].attention_status==Attention.RECENT
    assert status[walk].resolution_status==Resolution.OPEN
    assert status[active.focus_thread_id].attention_status==Attention.FOCUSED
    # Switching topics never completes or auto-answers an older question.
    with psycopg.connect(dsn,autocommit=True) as conn:
        q=conn.execute(
            """SELECT a.status,a.resolution_kind FROM dialogue_pending_acts a
               WHERE a.thread_id=%s""", (walk,),
        ).fetchone()
        assert q==("PENDING",None)


def test_explicit_reference_refocuses_unique_existing_thread(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "We can build a walking habit.",
        "Okay, recipes are another subject.",
        "Let's pick up the walking discussion.",
    )))
    host.begin_session("refocus")
    host.send("I want to walk more.",idempotency_key="walking")
    walk=state(dsn,identity).focus_thread_id
    host.send(
        "Let's discuss cooking instead.",idempotency_key="switch",
    )
    cooking=state(dsn,identity).focus_thread_id
    assert cooking!=walk
    host.restart_host()
    host.send(
        "Can we plan the next step for that goal?",
        idempotency_key="refer-to-walking",
    )
    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id==walk
    by_id={thread.thread_id:thread for thread in active.threads}
    assert by_id[cooking].attention_status==Attention.RECENT
    assert by_id[cooking].resolution_status==Resolution.OPEN


def test_delivered_choice_is_a_pending_decision_not_automatic_consent(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "Would you prefer walking indoors or outdoors?",
        "Okay, let's discuss what each would involve.",
    )))
    host.begin_session("choice")
    host.send("I want to walk more.",idempotency_key="start")
    first=state(dsn,identity)
    assert len(first.pending_acts)==1
    assert first.pending_acts[0].kind=="DECISION"
    assert "Await choice:" in first.threads[0].next_conversational_action

    # An unqualified acknowledgment does not resolve a decision and
    # cannot make an approved Goal/Task state from ordinary conversation.
    host.send("Okay",idempotency_key="not-approval")
    after=state(dsn,identity)
    assert [act.act_id for act in after.pending_acts]==[
        first.pending_acts[0].act_id
    ]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind,status FROM dialogue_pending_acts
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("AUTO_DELIVERED_DECISION","PENDING")
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0


def test_same_objective_reuses_topic_without_duplicating(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "We can discuss the garden.",
        "We still have that topic.",
    )))
    harness.begin_session("repeat")
    harness.send("I want to build a garden.",idempotency_key="first")
    original=state(dsn,identity).focus_thread_id
    harness.send("I want to build a garden.",idempotency_key="second")
    current=state(dsn,identity)
    assert len(current.threads)==1
    assert current.focus_thread_id==original
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==2


def test_generated_question_does_not_become_pending_until_delivery(dsn):
    identity=seed_local_demo(dsn)
    auth=TrustedActor(
        user_id=identity.user_id,source_id="mvp01c-test",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        first=ingest_text(
            conn,auth,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                text="I want to improve my cooking.",
                idempotency_key="not-delivered",
            ),
        )
        response=respond_to_text(
            conn,auth,first.turn_id,ReplyModel((
                "Which recipe would you like to try?",
            )),
        )
    before=state(dsn,identity)
    assert before.focus_thread_id is None
    assert before.threads==()
    assert before.pending_acts==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        acknowledge_text_delivery(conn,auth,response.response_id)
        replay=acknowledge_text_delivery(conn,auth,response.response_id)
    assert replay.replayed
    after=state(dsn,identity)
    assert len(after.threads)==1
    assert len(after.pending_acts)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_pending_acts
               WHERE source_response_id=%s""",
            (response.response_id,),
        ).fetchone()[0]==1


def test_literal_okay_and_ambiguous_reply_do_not_auto_answer_question(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "Which day would work?",
        "I can explain.",
        "We could discuss alternatives.",
    )))
    harness.begin_session("no-false-answers")
    harness.send("I want to walk daily.",idempotency_key="one")
    first=state(dsn,identity).pending_acts[0]
    harness.send("Okay",idempotency_key="okay")
    assert [a.act_id for a in state(dsn,identity).pending_acts]==[first.act_id]
    harness.send("What about weekends?",idempotency_key="question")
    assert [a.act_id for a in state(dsn,identity).pending_acts]==[first.act_id]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT resolution_kind FROM dialogue_pending_acts
               WHERE act_id=%s""", (first.act_id,),
        ).fetchone()==(None,)


def test_manual_thread_does_not_get_auto_question_or_duplicate_topic(dsn):
    from persona_agent.dialogue_contracts import OpenThreadCommand
    from persona_agent.dialogue_state import open_thread

    identity=seed_local_demo(dsn)
    auth=TrustedActor(user_id=identity.user_id,source_id="mvp01c-manual")
    with psycopg.connect(dsn,autocommit=True) as conn:
        incoming=ingest_text(
            conn,auth,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                text="I want to learn Python.",
                idempotency_key="manual-input",
            ),
        )
        open_thread(
            conn,auth,OpenThreadCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                expected_revision=1,evidence_turn_id=incoming.turn_id,
                thread_id=uuid4(),title="Explicit caller topic",
                idempotency_key="manual-open",
            ),
        )
        response=respond_to_text(
            conn,auth,incoming.turn_id,ReplyModel((
                "What project would you like to build?",
            )),
        )
        acknowledge_text_delivery(conn,auth,response.response_id)
    active=state(dsn,identity)
    assert len(active.threads)==1
    assert active.threads[0].title=="Explicit caller topic"
    assert active.pending_acts==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind FROM dialogue_threads
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("EXPLICIT",)


def test_model_proposed_objective_joins_same_dialogue_flow(dsn):
    from persona_agent.conversation_contracts import ModelIntentCandidate

    class WithIntent:
        def generate_interpreted(self,messages):
            message=messages[-1].content
            return ModelReply(
                model_id="mvp01c-model",text="Which day would suit you?",
                intent_candidate=ModelIntentCandidate(
                    kind="OBJECTIVE",evidence_quote=message,
                    confidence=0.93,reference="NONE",
                ),
            )
        def generate(self,messages):
            raise AssertionError("a second inference must not be necessary")

    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,WithIntent())
    host.begin_session("model-cue")
    host.send(
        "Lately I've been thinking about a regular walking routine.",
        idempotency_key="implicit-goal",
    )
    active=state(dsn,identity)
    assert len(active.threads)==1
    assert len(active.pending_acts)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT epistemic_status FROM conversation_intent_signals
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("MODEL_PROPOSED",)
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0


def test_multiple_sessions_do_not_share_dialogue_focus(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "What would help you?",
        "Which route?",
    )))
    first=harness.begin_session("one")
    harness.send("I want to eat healthier.",idempotency_key="food")
    first_state=state(dsn,first)
    assert first_state.focus_thread_id is not None
    second=harness.begin_session("two")
    harness.send("I want to walk more.",idempotency_key="walking")
    second_state=state(dsn,second)
    assert second_state.focus_thread_id is not None
    assert second_state.focus_thread_id!=first_state.focus_thread_id
    assert state(dsn,first).focus_thread_id==first_state.focus_thread_id
