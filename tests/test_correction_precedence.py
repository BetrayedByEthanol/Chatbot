"""PostgreSQL evidence tests for corrected discussion cadence, not Goal approval.

Drives real authenticated conversation ingress and inspects model-facing context.
No test helper creates or changes a Goal, Memory record or dialogue link.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.intent_interpretation import _FREQUENCY_CORRECTION

ORIGINAL = "I want to walk three times a week."
CORRECTION = "Actually, twice a week is more realistic."
RECALL = "Was my latest walking target two times a week or three?"


class ContextObserver:
    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(tuple(messages))
        return ModelReply(
            model_id="correction-precedence-test",
            text="I can discuss that, without assuming you approved a goal.",
        )


def fixture(dsn):
    model = ContextObserver()
    host = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), model)
    host.begin_session("first")
    return host, model


def system(model):
    return model.calls[-1][0].content


@pytest.mark.parametrize("text", [
    CORRECTION,
    "Actually, twice per week would be better.",
    "Actually, once per week is better.",
    "Actually, three times a week is more realistic.",
])
def test_high_precision_cadence_correction_variants(text):
    assert _FREQUENCY_CORRECTION.fullmatch(text) is not None


@pytest.mark.parametrize("text", [
    "Actually, twice a week is more realistic for my colleague.",
    "Actually, twice a week would be good for Nginx.",
    "Actually, maybe twice a week.",
    "I want to walk twice a week.",
])
def test_unrelated_statements_are_not_cadence_corrections(text):
    assert _FREQUENCY_CORRECTION.fullmatch(text) is None


def test_realistic_correction_links_to_one_objective_without_goal_admission(dsn):
    host, model = fixture(dsn)
    original = host.send(ORIGINAL, idempotency_key="objective")
    correction = host.send(CORRECTION, idempotency_key="correction")
    with psycopg.connect(dsn, autocommit=True) as conn:
        signals = conn.execute(
            """SELECT s.signal_id,s.intent_kind,s.related_signal_id,
                      s.proposal_status,s.epistemic_status,
                      t.input_text,t.status
               FROM conversation_intent_signals s
               JOIN conversation_turns t ON t.turn_id=s.turn_id
               WHERE s.user_id=%s AND s.instance_id=%s
                 AND s.conversation_id=%s
               ORDER BY s.turn_sequence""",
            (host.identity.user_id, host.identity.instance_id,
             host.identity.conversation_id),
        ).fetchall()
        goals = conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (host.identity.user_id, host.identity.instance_id),
        ).fetchone()[0]
    assert [s[1] for s in signals] == ["OBJECTIVE", "CORRECTION"]
    assert signals[0][2] is None
    assert signals[1][2] == signals[0][0]
    assert [s[3:5] for s in signals] == [
        ("UNREVIEWED", "RULE_INFERRED"),
        ("UNREVIEWED", "RULE_INFERRED"),
    ]
    assert signals[0][5:] == (ORIGINAL, "RESPONDED")
    assert signals[1][5:] == (CORRECTION, "RESPONDED")
    assert original.turn_id != correction.turn_id
    assert goals == 0
    # The current correction turn also supplies its linked origin to inference.
    assert "Linked user corrections revise" in system(model)
    assert "walk three times a week" in system(model)
    assert CORRECTION in system(model)


def test_latest_linked_correction_survives_new_session_and_host_restart(dsn):
    host, model = fixture(dsn)
    source = host.send(ORIGINAL, idempotency_key="source")
    updated = host.send(CORRECTION, idempotency_key="updated")
    host.begin_session("later")
    host.restart_host()
    host.send(RECALL, idempotency_key="recall")
    prompt = system(model)
    assert "Source-linked conversation correction" in prompt
    block = prompt.split("Source-linked conversation correction", 1)[1]
    assert ORIGINAL in block
    assert CORRECTION in block
    assert source.turn_id in block
    assert updated.turn_id in block
    assert block.index(ORIGINAL) < block.index(CORRECTION)
    assert "latest_linked_correction" in block
    assert "NOT_INFERRED_FROM_DISCUSSION" in block
    assert "do not present both cadences as equally current" in block
    assert len(prompt) <= 12000
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s AND instance_id=%s",
            (host.identity.user_id, host.identity.instance_id),
        ).fetchone()[0] == 0


def test_last_of_successive_linked_corrections_takes_precedence(dsn):
    host, model = fixture(dsn)
    source = host.send(ORIGINAL, idempotency_key="source")
    first = host.send(CORRECTION, idempotency_key="first")
    last_text = "Actually, once per week would be better."
    last = host.send(last_text, idempotency_key="last")
    with psycopg.connect(dsn, autocommit=True) as conn:
        links = conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE turn_id IN (%s,%s) ORDER BY turn_sequence""",
            (first.turn_id, last.turn_id),
        ).fetchall()
        root = conn.execute(
            "SELECT signal_id FROM conversation_intent_signals WHERE turn_id=%s",
            (source.turn_id,),
        ).fetchone()[0]
    assert links == [(root,), (root,)]
    host.begin_session("later")
    host.send("What was my walking plan?", idempotency_key="recall")
    block = system(model).split("Source-linked conversation correction", 1)[1]
    assert last_text in block and last.turn_id in block
    assert CORRECTION not in block  # No earlier correction masquerades as latest.


def test_ambiguous_duplicate_objectives_abstain_instead_of_guessing(dsn):
    host, model = fixture(dsn)
    host.send(ORIGINAL, idempotency_key="first-walking-objective")
    host.send("I want to walk five times a week.", idempotency_key="second-walking")
    host.send(CORRECTION, idempotency_key="correct-second")
    host.begin_session("later")
    host.send(RECALL, idempotency_key="ambiguous-recall")
    assert "Source-linked conversation correction" not in system(model)


def test_topic_switch_does_not_link_frequency_to_old_objective(dsn):
    host, model = fixture(dsn)
    host.send(ORIGINAL, idempotency_key="walking")
    host.send("Switch topic to cooking.", idempotency_key="switch")
    updated = host.send(CORRECTION, idempotency_key="ambiguous-correction")
    with psycopg.connect(dsn, autocommit=True) as conn:
        related = conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE turn_id=%s""", (updated.turn_id,),
        ).fetchone()[0]
    assert related is None
    host.begin_session("later")
    host.send(RECALL, idempotency_key="recall")
    assert "Source-linked conversation correction" not in system(model)


def test_other_users_never_receive_linked_correction(dsn):
    first, _ = fixture(dsn)
    first.send(ORIGINAL, idempotency_key="original")
    first.send(CORRECTION, idempotency_key="correction")
    other_model = ContextObserver()
    other = BehavioralAcceptanceHarness(
        dsn, seed_local_demo(dsn), other_model,
    )
    other.begin_session("other-user")
    other.send(RECALL, idempotency_key="recall")
    assert ORIGINAL not in system(other_model)
    assert CORRECTION not in system(other_model)
    assert "Source-linked conversation correction" not in system(other_model)


def test_newer_unlinked_cadence_after_eight_turn_window_forces_abstention(dsn):
    """Regression: seq 1 objective, seq 2 linked correction, seq 10 unlinked.

    Previously "latest linked" incorrectly presented the obsolete twice-weekly
    amendment even though Conversation persisted a newer correction.
    """
    host, model = fixture(dsn)
    root = host.send(ORIGINAL, idempotency_key="objective-1")
    earlier = host.send(CORRECTION, idempotency_key="correction-2")
    for index in range(3, 10):
        host.send(
            f"We are still talking about walking, note number {index}.",
            idempotency_key=f"note-{index}",
        )
    latest_text = "Actually, once per week would be better."
    latest = host.send(latest_text, idempotency_key="correction-10")
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """SELECT turn_id,turn_sequence,intent_kind,related_signal_id
               FROM conversation_intent_signals
               WHERE user_id=%s AND instance_id=%s AND conversation_id=%s
                 AND intent_kind IN ('OBJECTIVE','CORRECTION')
               ORDER BY turn_sequence""",
            (host.identity.user_id, host.identity.instance_id,
             host.identity.conversation_id),
        ).fetchall()
    # Assert the precise evidence condition instead of only the resulting
    # model text: the new amendment really did lose its source link.
    assert [row[1] for row in rows] == [1, 2, 10]
    assert str(rows[0][0]) == root.turn_id
    assert str(rows[1][0]) == earlier.turn_id
    assert str(rows[2][0]) == latest.turn_id
    assert rows[1][3] is not None
    assert rows[2][3] is None
    host.begin_session("later")
    host.restart_host()
    host.send(RECALL, idempotency_key="cross-session-recall")
    assert "Source-linked conversation correction" not in system(model)
    with psycopg.connect(dsn, autocommit=True) as conn:
        count = conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (host.identity.user_id, host.identity.instance_id),
        ).fetchone()[0]
    assert count == 0


def test_non_cadence_unlinked_correction_does_not_mask_valid_link(dsn):
    host, model = fixture(dsn)
    host.send(ORIGINAL, idempotency_key="objective")
    host.send(CORRECTION, idempotency_key="linked")
    # An unrelated correction without a weekly frequency should not
    # automatically invalidate a source-linked walking cadence.
    host.send(
        "Actually, the keyboard belongs to someone else.",
        idempotency_key="unrelated-correction",
    )
    host.begin_session("later")
    host.send(RECALL, idempotency_key="recall")
    assert "Source-linked conversation correction" in system(model)


def test_recent_linked_correction_remains_available_after_unrelated_turns(dsn):
    host, model = fixture(dsn)
    host.send(ORIGINAL, idempotency_key="objective")
    host.send(CORRECTION, idempotency_key="linked")
    for index in range(3, 11):
        host.send(
            f"Let's keep walking in mind for later, note {index}.",
            idempotency_key=f"continuation-{index}",
        )
    host.begin_session("later")
    host.send(RECALL, idempotency_key="recall")
    prompt = system(model)
    assert "Source-linked conversation correction" in prompt
    assert CORRECTION in prompt
