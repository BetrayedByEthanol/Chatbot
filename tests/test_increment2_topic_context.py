"""Increment 2: explicit topic transitions and source-linked discussion resumption.

Real authenticated Conversation/Dialogue integration, not fabricated prompt
strings, model assertions, Goal approvals or test-only state mutations.
"""
from __future__ import annotations

import re
import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.contracts import TrustedActor
from persona_agent.dialogue_state import load_active_dialogue
from persona_agent.event_delivery import dispatch_outbox
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


def test_resumed_topic_abstains_on_late_unlinked_frequency_revision(dsn):
    h, model = host(dsn)
    original = "I want to walk three times a week."
    first_correction = "Actually, twice a week is more realistic."
    latest_correction = "Actually, once per week would be better."
    h.send(original, idempotency_key="original")
    h.send(first_correction, idempotency_key="linked")
    for index in range(3, 10):
        h.send(
            f"An unrelated thought about colors number {index}.",
            idempotency_key=f"filler-{index}",
        )
    latest = h.send(latest_correction, idempotency_key="unlinked")
    with psycopg.connect(dsn, autocommit=True) as conn:
        signal = conn.execute(
            """SELECT related_signal_id,turn_sequence FROM conversation_intent_signals
               WHERE turn_id=%s""",
            (latest.turn_id,),
        ).fetchone()
        assert signal == (None, 10)
    h.begin_session("later")
    h.send("Back to walking.", idempotency_key="return")
    system = prompt(model)
    assert "Earlier source-linked user statements" not in system
    assert original not in system
    assert first_correction not in system
    assert latest_correction not in system


@pytest.mark.parametrize("utterance", [
    "Let's go back to that topic",
    "Can we go back to it?",
    "Back to it.",
    "Let's return to this discussion.",
    "Let's go back to that plan",
    "Where did we leave off with that topic?",
    "Where had I stopped with it?",
])
def test_anaphoric_returns_are_not_named_topics(utterance):
    assert parse_explicit_topic_return(utterance) is None
    assert explicit_topic_history_request(utterance) is None


@pytest.mark.parametrize("utterance", [
    "Let's go back to that topic",
    "Can we go back to it?",
])
def test_anaphoric_returns_keep_existing_focus_and_prompt(dsn, utterance):
    h, model = host(dsn)
    identity = h.active_identity
    h.send("Let's switch topics to Nginx certificates.",
           idempotency_key="nginx")
    before = dialogue(dsn, identity)
    assert before.focus_thread_id is not None
    h.send(utterance, idempotency_key="anaphoric")
    after = dialogue(dsn, identity)
    assert after.focus_thread_id == before.focus_thread_id
    assert len(after.threads) == len(before.threads) == 1
    assert after.threads[0].title == "Nginx certificates"
    current_prompt = prompt(model)
    assert "Current user turn explicitly names a topic transition" not in current_prompt
    assert "Stored dialogue metadata" in current_prompt
    assert "Earlier source-linked user statements" not in current_prompt


def test_intervening_unrelated_turn_does_not_drop_linked_correction(dsn):
    h, model = host(dsn)
    original = "I want to walk three times a week."
    corrected = "Actually, twice a week is more realistic."
    source = h.send(original, idempotency_key="original")
    h.send("I made tea this evening.", idempotency_key="tea")
    amended = h.send(corrected, idempotency_key="correction")
    with psycopg.connect(dsn, autocommit=True) as conn:
        root = conn.execute(
            "SELECT signal_id FROM conversation_intent_signals WHERE turn_id=%s",
            (source.turn_id,),
        ).fetchone()[0]
        related = conn.execute(
            "SELECT related_signal_id FROM conversation_intent_signals WHERE turn_id=%s",
            (amended.turn_id,),
        ).fetchone()[0]
    assert related == root  # Amendment is linked, despite the tea interruption.
    h.begin_session("later")
    h.restart_host()
    h.send("Back to walking.", idempotency_key="return-walking")
    current = prompt(model)
    assert "Earlier source-linked user statements" in current
    assert original in current and source.turn_id in current
    assert corrected in current and amended.turn_id in current
    assert current.index(original) < current.index(corrected)
    assert "latest linked discussion correction" in current
    assert "not a finalized plan or approved goal" in current


def test_latest_linked_correction_wins_across_intervening_turns(dsn):
    h, model = host(dsn)
    original = "I want to walk three times a week."
    earlier = "Actually, twice a week is more realistic."
    latest = "Actually, once per week would be better."
    h.send(original, idempotency_key="original")
    h.send(earlier, idempotency_key="first")
    h.send("I made tea this evening.", idempotency_key="tea")
    newest = h.send(latest, idempotency_key="latest")
    h.begin_session("later")
    h.send("Back to walking.", idempotency_key="resume")
    current = prompt(model)
    assert original in current
    assert latest in current and newest.turn_id in current
    assert earlier not in current


def test_resumed_linked_correction_is_atomic_under_small_budget(dsn):
    from psycopg.rows import dict_row
    from persona_agent.historical_user_turns import historical_user_turn_context_block

    h, model = host(dsn)
    original = "I want to walk three times a week."
    correction = "Actually, twice a week is more realistic."
    h.send(original, idempotency_key="original")
    h.send("I made tea this evening.", idempotency_key="tea")
    h.send(correction, idempotency_key="correction")
    h.begin_session("later")
    recall = h.send("Back to walking.", idempotency_key="return")
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        at = conn.execute(
            "SELECT created_at FROM conversation_turns WHERE turn_id=%s",
            (recall.turn_id,),
        ).fetchone()["created_at"]
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                def read(limit):
                    return historical_user_turn_context_block(
                        cur,user_id=h.identity.user_id,
                        instance_id=h.identity.instance_id,
                        conversation_id=h.active_identity.conversation_id,
                        before_at=at,latest_text="Back to walking.",
                        max_chars=limit,
                    )
                full = read(1400)
                assert original in full and correction in full
                constrained = read(len(full)-1)
    # Never show a stale first cadence in isolation when the newer linked
    # correction cannot fit. The whole source+latest amendment is atomic.
    assert original not in constrained
    assert correction not in constrained
    assert not constrained or len(constrained) <= len(full)-1


@pytest.mark.parametrize("utterance", [
    "Let's switch to it.",
    "Switch the topic to that topic.",
])
def test_deictic_switches_also_do_not_open_literal_topics(utterance):
    assert parse_explicit_topic_switch(utterance) is None


def test_anaphoric_return_does_not_answer_pending_question(dsn):
    class QuestionModel(CaptureModel):
        def generate(self, messages):
            self.calls.append(tuple(messages))
            return ModelReply(
                model_id="increment2.question-model",
                text=("What would you like to do next?" if len(self.calls) == 1
                      else "We can continue."),
            )

    model = QuestionModel()
    h = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), model)
    h.begin_session("topic")
    identity = h.active_identity
    h.send("Let's switch topics to Nginx certificates.",
           idempotency_key="topic")
    before = dialogue(dsn, identity)
    assert before.focus_thread_id is not None
    assert len(before.pending_acts) == 1
    old_act = before.pending_acts[0].act_id

    h.send("Let's go back to that topic", idempotency_key="anaphora")
    after = dialogue(dsn, identity)
    assert after.focus_thread_id == before.focus_thread_id
    assert len(after.threads) == 1
    assert len(after.pending_acts) == 1
    assert after.pending_acts[0].act_id == old_act
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM dialogue_pending_acts WHERE act_id=%s",
            (old_act,),
        ).fetchone()[0] == "PENDING"


def test_explicit_nginx_followup_outweighs_recent_walking_objective(dsn):
    """The generic 'next step' phrase must not override explicit topic intent."""
    h, model = host(dsn)
    identity = h.active_identity
    h.send("I want to walk three times a week.", idempotency_key="walk")
    walking_thread = dialogue(dsn, identity).focus_thread_id
    h.send("Let's switch topics to Nginx certificates.", idempotency_key="nginx")
    nginx_thread = dialogue(dsn, identity).focus_thread_id
    assert nginx_thread != walking_thread

    followup = "Back to Nginx: what's the next step?"
    turn = h.send(followup, idempotency_key="nginx-next-step")
    state = dialogue(dsn, identity)
    assert state.focus_thread_id == nginx_thread
    assert len(state.threads) == 2
    current = prompt(model)
    assert "Current user turn explicitly names a topic transition" in current
    assert "Resolved prior conversation reference" not in current
    assert model.calls[-1][-1].content == followup
    with psycopg.connect(dsn, autocommit=True) as conn:
        linked = conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
            (turn.turn_id, identity.user_id, identity.instance_id),
        ).fetchone()
        assert linked == (None,)
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id, identity.instance_id),
        ).fetchone()[0] == 0


def test_ambiguous_named_plan_return_does_not_create_third_topic(dsn):
    """An unresolved named-plan reference must retain its abstention contract."""
    h, model = host(dsn)
    identity = h.active_identity
    h.send("I want to walk every morning.", idempotency_key="walk-one")
    first = dialogue(dsn, identity).focus_thread_id
    h.send("I want to walk at weekends.", idempotency_key="walk-two")
    second = dialogue(dsn, identity).focus_thread_id
    assert first != second
    h.send("Let's switch topics to Nginx certificates.",
           idempotency_key="nginx")
    focused = dialogue(dsn, identity).focus_thread_id
    assert focused not in (first, second)

    turn = h.send("Can we return to our walking plan?",
                  idempotency_key="ambiguous-plan-return")
    state = dialogue(dsn, identity)
    assert state.focus_thread_id == focused
    assert len(state.threads) == 3
    assert all(thread.title != "our walking plan"
               for thread in state.threads)
    assert "Resolved prior conversation reference" not in prompt(model)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
            (turn.turn_id, identity.user_id, identity.instance_id),
        ).fetchone() == (None,)
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (turn.turn_id, identity.conversation_id),
        ).fetchone()[0] == 0


def test_uniquely_resolved_named_plan_still_returns_to_objective(dsn):
    """An exact, unambiguous named goal reference keeps the old behavior."""
    h, model = host(dsn)
    identity = h.active_identity
    h.send("I want to walk every morning.", idempotency_key="walk")
    walking_thread = dialogue(dsn, identity).focus_thread_id
    h.send("Let's switch topics to Nginx certificates.", idempotency_key="nginx")
    assert dialogue(dsn, identity).focus_thread_id != walking_thread

    turn = h.send("Can we return to our walk plan?",
                  idempotency_key="unique-walk-plan")
    state = dialogue(dsn, identity)
    assert state.focus_thread_id == walking_thread
    assert len(state.threads) == 2
    assert "Resolved prior conversation reference" in prompt(model)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            """SELECT related_signal_id IS NOT NULL FROM conversation_intent_signals
               WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
            (turn.turn_id, identity.user_id, identity.instance_id),
        ).fetchone() == (True,)


@pytest.mark.parametrize("bare_returns", [3, 18])
def test_bare_topic_returns_cannot_crowd_out_substantive_progress(
    dsn, bare_returns,
):
    """Filter navigation BEFORE the 16-candidate/3-result FTS limits.

    The 3-return case reproduces the review report; the 18-return case
    verifies that post-LIMIT filtering would still be insufficient.
    """
    h, model = host(dsn)
    switch = h.send(
        "Let's switch topics to Nginx certificates.",
        idempotency_key="initial-topic",
    )
    progress = h.send(SOURCE, idempotency_key="certificate-progress")
    bare_turns = []
    for index in range(bare_returns):
        bare_turns.append(
            h.send("Back to Nginx.", idempotency_key=f"bare-return-{index}")
        )
    # Starting a new session places the progress outside the recent history
    # without creating dozens of unrelated turns and excess outbox traffic.
    h.begin_session("later")
    h.restart_host()
    h.send("Back to Nginx.", idempotency_key="retrieve-progress")
    context = prompt(model)
    assert "Earlier source-linked user statements" in context
    assert progress.turn_id in context and SOURCE in context
    # The CURRENT input is correctly echoed in intent context, so do not
    # forbid the literal phrase everywhere in the system prompt. Assert
    # that no previous navigation turn is quoted as historical evidence.
    assert switch.turn_id not in context
    assert all(turn.turn_id not in context for turn in bare_turns)
    assert 'USER_REPORTED source turn' in context
    assert not re.search(
        r'USER_REPORTED (?:source turn|discussion update source turn)[^\\n]*'
        r'"Back to Nginx\\."',
        context,
    )
    assert sum(len(m.content) for m in model.calls[-1]) <= 12000

    # Dispatch the authenticated test events through the real event API.
    # Large repeated-turn cases otherwise leave >100 stream-order rounds
    # for a later unrelated memory-worker test's bounded setup.
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(250):
            if dispatch_outbox(conn, limit=1000) == 0:
                break
        else:
            pytest.fail("repeated-return test left an excessive outbox backlog")


def test_return_with_substantive_update_is_still_retrieved(dsn):
    h, model = host(dsn)
    h.send("Let's switch topics to Nginx certificates.",
           idempotency_key="initial-topic")
    substantive = "Back to Nginx: I generated the certificate but have not installed it."
    progress = h.send(substantive, idempotency_key="substantive-update")
    for index in range(13):
        h.send(f"A separate thought about tea number {index}.",
               idempotency_key=f"tea-{index}")
    h.send("Back to Nginx.", idempotency_key="resume")
    context = prompt(model)
    assert "Earlier source-linked user statements" in context
    assert progress.turn_id in context
    assert substantive in context
