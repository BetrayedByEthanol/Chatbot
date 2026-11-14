"""MVP-00A: behavioral evidence via the normal selected-persona conversation host.

Mock model text tests runtime contracts; they do not claim real-model
interpretation quality. Domain observations are read-only.
"""
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply


class EchoModel:
    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(messages)
        return ModelReply(
            model_id="mvp00a-echo",
            text="Received: " + messages[-1].content,
        )


def test_sessions_share_persona_but_not_conversation_and_survive_host_restart(dsn):
    identity = seed_local_demo(dsn)
    model = EchoModel()
    harness = BehavioralAcceptanceHarness(dsn, identity, model)

    first_id = harness.begin_session("goal")
    first = harness.send(
        "I want to eat healthier. Can you help me with this goal?",
        idempotency_key="mvp00a-goal-request",
    )
    harness.send(
        "Yes, please help me make this a lasting goal.",
        idempotency_key="mvp00a-goal-approval",
    )
    harness.restart_host()
    harness.send(
        "What did we just discuss?",
        idempotency_key="mvp00a-goal-continuity",
    )

    second_id = harness.begin_session("preferences")
    assert first_id.conversation_id != second_id.conversation_id
    assert first_id.user_id == second_id.user_id == identity.user_id
    assert first_id.instance_id == second_id.instance_id == identity.instance_id
    another = harness.send(
        "My favorite drink is tea.",
        idempotency_key="mvp00a-preference",
    )
    assert first.session == "goal" and another.session == "preferences"
    assert len(model.calls) == 4
    assert all(item["type"] == "reply" for item in harness.deliveries)
    observed = harness.observe_state()
    assert observed["conversations"] == 2
    assert observed["turns"] == 4
    assert observed["delivered_responses"] == 4
    # Natural-language goal integration is not implemented by the test.
    assert observed["goals"] == 0


def test_existing_extraction_worker_accepts_input_evidence_across_sessions(dsn):
    identity = seed_local_demo(dsn)
    harness = BehavioralAcceptanceHarness(dsn, identity, EchoModel())
    harness.enable_explicit_memory_worker()
    source = harness.begin_session("preferences")
    saved = harness.send(
        "My favorite drink is tea.",
        idempotency_key="mvp00a-memory",
    )
    tick = harness.tick_explicit_memory_worker()
    assert tick["admitted"] >= 1
    assert harness.observe_state()["memories"] == 1

    # The harness never inserts memory data: provenance comes from the
    # authenticated conversation event processed by the existing worker.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            """SELECT source_conversation_id, source_turn_id, source_quote,
                      epistemic_status
               FROM memory_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id, identity.instance_id),
        ).fetchone()
    assert row == (
        source.conversation_id, saved.turn_id and __import__("uuid").UUID(saved.turn_id),
        "My favorite drink is tea.", "USER_REPORTED",
    )

    harness.begin_session("recall")
    harness.send(
        "Do you remember my favorite drink?",
        idempotency_key="mvp00a-recall",
    )
    harness.restart_host()
    harness.tick_explicit_memory_worker()
    observed = harness.observe_state()
    assert observed["conversations"] == 2
    assert observed["memories"] == 1
    assert observed["turns"] == 2


def test_idempotent_replay_after_restart_does_not_add_new_user_turn(dsn):
    identity = seed_local_demo(dsn)
    harness = BehavioralAcceptanceHarness(dsn, identity, EchoModel())
    harness.begin_session("replay")
    first = harness.send(
        "My hobby is gardening.",
        idempotency_key="mvp00a-stable-turn",
    )
    harness.restart_host()
    replay = harness.send(
        "My hobby is gardening.",
        idempotency_key="mvp00a-stable-turn",
    )
    assert replay.turn_id == first.turn_id
    assert replay.response_id == first.response_id
    assert harness.observe_state()["turns"] == 1
    assert harness.observe_state()["delivered_responses"] == 1


def test_observation_counts_are_scoped_to_the_active_test_persona(dsn):
    first = seed_local_demo(dsn)
    second = seed_local_demo(dsn)
    a = BehavioralAcceptanceHarness(dsn, first, EchoModel())
    b = BehavioralAcceptanceHarness(dsn, second, EchoModel())
    a.begin_session("one")
    b.begin_session("two")
    a.send("Hello", idempotency_key="mvp00a-first")
    assert a.observe_state()["turns"] == 1
    assert b.observe_state()["turns"] == 0
    b.send("Hello", idempotency_key="mvp00a-second")
    assert b.observe_state()["turns"] == 1
    assert a.observe_state()["turns"] == 1
