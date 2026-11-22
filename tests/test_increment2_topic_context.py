"""Increment 2: explicit topic transitions and source-linked discussion resumption.

Real authenticated Conversation/Dialogue integration, not fabricated prompt
strings, model assertions, Goal approvals or test-only state mutations.
"""
from __future__ import annotations

import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.contracts import TrustedActor
from persona_agent.dialogue_state import load_active_dialogue
from persona_agent.intent_interpretation import (
    classify_intent, parse_explicit_topic_switch,
    parse_explicit_topic_return, explicit_topic_history_request,
)

SOURCE = "I generated a certificate locally, but I haven't installed it in Nginx."


class CaptureModel:
    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(tuple(messages))
        return ModelReply(
            model_id="increment2.capture",
            text="We can continue discussing that without claiming I ran any commands.",
        )


def host(dsn):
    model = CaptureModel()
    h = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), model)
    h.begin_session("discussion")
    return h, model


def prompt(model):
    return model.calls[-1][0].content


def dialogue(dsn, identity):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return load_active_dialogue(
            conn, TrustedActor(user_id=identity.user_id, source_id="increment2-test"),
            identity.conversation_id,
        )


@pytest.mark.parametrize("utterance,topic", [
    ("Let's switch topics to Nginx certificates.", "Nginx certificates"),
    ("Let's switch to Nginx.", "Nginx"),
    ("Switch topic to cooking.", "cooking"),
    ("Change the topic to gardening.", "gardening"),
    ("New topic: astronomy.", "astronomy"),
])
def test_explicit_switch_variants_admitted_as_requests(utterance, topic):
    assert parse_explicit_topic_switch(utterance) == topic
    assert classify_intent(utterance).kind == "REQUEST"
    assert explicit_topic_history_request(utterance) is None


@pytest.mark.parametrize("utterance,topic", [
    ("Back to Nginx.", "Nginx"),
    ("Anyway, back to walking.", "walking"),
    ("Let's go back to Nginx certificates.", "Nginx certificates"),
    ("Let's return to walking.", "walking"),
    ("Can we go back to Nginx?", "Nginx"),
    ("Back to Nginx: where had I stopped?", "Nginx"),
])
def test_named_return_variants_and_history_gates(utterance, topic):
    assert parse_explicit_topic_return(utterance) == topic
    assert explicit_topic_history_request(utterance) == topic
    assert classify_intent(utterance).kind == "REQUEST"


@pytest.mark.parametrize("utterance,topic", [
    ("Where did we leave off with Nginx?", "Nginx"),
    ("Where had I stopped with Nginx certificates?", "Nginx certificates"),
])
def test_explicit_progress_recall_without_remember_keyword(utterance, topic):
    assert parse_explicit_topic_return(utterance) is None
    assert explicit_topic_history_request(utterance) == topic


@pytest.mark.parametrize("utterance", [
    "What if we switched topics to Nginx?",
    "Back to",
    "Let's switch topics to",
    "Let's go back",
    "Could you explain Nginx configuration?",
    "I like Nginx.",
    "How do I install Nginx?",
])
def test_ordinary_or_incomplete_inputs_do_not_request_history(utterance):
    assert explicit_topic_history_request(utterance) is None


def test_switch_and_resume_reuse_unique_threads_without_premature_focus(dsn):
    h, model = host(dsn)
    identity = h.active_identity
    h.send("I want to walk three times a week.", idempotency_key="walking")
    first = dialogue(dsn, identity)
    walking = first.focus_thread_id
    assert walking is not None

    h.send("Let's switch topics to Nginx certificates.", idempotency_key="nginx")
    # The generation prompt must not mislabel the previous walking focus as
    # current when the user explicitly switched topics in this turn.
    first_switch_prompt = prompt(model)
    assert "Current user turn explicitly names a topic transition" in first_switch_prompt
    assert "Active dialogue:" not in first_switch_prompt
    second = dialogue(dsn, identity)
    nginx = second.focus_thread_id
    assert nginx is not None and nginx != walking
    assert len(second.threads) == 2
    assert any(t.thread_id == nginx and t.title == "Nginx certificates"
               for t in second.threads)

    h.restart_host()
    h.send("Anyway, back to walking.", idempotency_key="return-walking")
    resumed = dialogue(dsn, identity)
    assert resumed.focus_thread_id == walking
    assert len(resumed.threads) == 2

    h.send("Back to Nginx: where had I stopped?", idempotency_key="return-nginx")
    back = dialogue(dsn, identity)
    assert back.focus_thread_id == nginx
    assert len(back.threads) == 2
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (identity.user_id, identity.instance_id),
        ).fetchone()[0] == 0


def test_ambiguous_topic_return_abstains_without_duplicate_or_refocus(dsn):
    h, model = host(dsn)
    identity = h.active_identity
    h.send("Let's switch topics to Nginx certificates.", idempotency_key="cert")
    cert = dialogue(dsn, identity).focus_thread_id
    h.send("Let's switch topics to Nginx configuration.", idempotency_key="config")
    config = dialogue(dsn, identity).focus_thread_id
    assert config != cert
    h.send("Let's switch topics to gardening.", idempotency_key="garden")
    garden = dialogue(dsn, identity).focus_thread_id

    h.send("Back to Nginx.", idempotency_key="ambiguous")
    state = dialogue(dsn, identity)
    assert state.focus_thread_id == garden
    assert len(state.threads) == 3
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE turn_id=(SELECT turn_id FROM conversation_turns
                              WHERE conversation_id=%s
                              ORDER BY sequence DESC LIMIT 1)""",
            (identity.conversation_id,),
        ).fetchone()[0] == 0


def test_cross_session_nginx_progress_recalled_from_original_user_turn(dsn):
    h, model = host(dsn)
    h.send("Let's switch topics to Nginx certificates.", idempotency_key="nginx")
    earlier = h.send(SOURCE, idempotency_key="progress")
    h.begin_session("later")
    h.restart_host()
    h.send("Back to Nginx: where had I stopped?", idempotency_key="resume")
    system = prompt(model)
    assert "Earlier source-linked user statements" in system
    assert SOURCE in system
    assert earlier.turn_id in system
    assert "USER_REPORTED" in system
    assert "not verified facts or permissions" in system
    assert "not an approved" in system or "not a finalized plan or approved" in system
    assert model.calls[-1][-1].content == "Back to Nginx: where had I stopped?"
    # No historical transcript masquerading as an ordinary current user turn.
    assert all(m.content != SOURCE for m in model.calls[-1][1:])
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (h.identity.user_id, h.identity.instance_id),
        ).fetchone()[0] == 0


def test_interrupted_same_session_progress_outside_recent_window_is_retrieved(dsn):
    h, model = host(dsn)
    h.send("Let's switch topics to Nginx certificates.", idempotency_key="nginx")
    earlier = h.send(SOURCE, idempotency_key="progress")
    for i in range(15):
        h.send(f"A separate thought about tea number {i}.",
               idempotency_key=f"tea-{i}")
    h.send("Where did we leave off with Nginx?", idempotency_key="resume")
    system = prompt(model)
    assert "Earlier source-linked user statements" in system
    assert SOURCE in system
    assert earlier.turn_id in system
    assert "Active dialogue:" not in system


def test_new_topic_or_unrelated_message_does_not_pull_old_evidence(dsn):
    h, model = host(dsn)
    h.send(SOURCE, idempotency_key="source")
    h.begin_session("later")
    h.send("What's the best pasta recipe?", idempotency_key="ordinary")
    assert "Earlier source-linked user statements" not in prompt(model)
    h.send("Let's switch to gardening.", idempotency_key="new-topic")
    assert "Earlier source-linked user statements" not in prompt(model)
    assert SOURCE not in prompt(model)


def test_topic_return_does_not_leak_other_users_conversation(dsn):
    h, _ = host(dsn)
    evidence = h.send(SOURCE, idempotency_key="source")
    other, model = host(dsn)
    other.send("Back to Nginx: where had I stopped?",
               idempotency_key="other-user-recall")
    assert SOURCE not in prompt(model)
    assert evidence.turn_id not in prompt(model)
    assert "Earlier source-linked user statements" not in prompt(model)


def test_named_return_carries_source_linked_walking_correction(dsn):
    h, model = host(dsn)
    original = "I want to walk three times a week."
    corrected = "Actually, twice a week is more realistic."
    source = h.send(original, idempotency_key="original")
    newer = h.send(corrected, idempotency_key="corrected")
    h.begin_session("later")
    h.restart_host()
    h.send("Back to walking.", idempotency_key="return")
    current = prompt(model)
    assert "Earlier source-linked user statements" in current
    assert original in current and source.turn_id in current
    assert corrected in current and newer.turn_id in current
    assert current.index(original) < current.index(corrected)
    assert "not verified facts or permissions" in current
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (h.identity.user_id, h.identity.instance_id),
        ).fetchone()[0] == 0
