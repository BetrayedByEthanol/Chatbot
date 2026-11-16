"""MVP-02B.1: PostgreSQL + normal-host historical evidence acceptance."""
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.contracts import TrustedActor
from persona_agent.memory import admit_memory, amend_memory
from persona_agent.memory_contracts import AdmitMemoryCommand, AmendMemoryCommand


class CaptureModel:
    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(tuple(messages))
        return ModelReply(
            model_id="mvp02b1.capture",
            text="I can discuss that without inventing historical details.",
        )


def host(dsn):
    model = CaptureModel()
    harness = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), model)
    return harness, model


def system(model):
    assert model.calls
    return model.calls[-1][0].content


def test_explicit_cross_session_recall_quotes_user_not_assistant(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    statement = "I took a short evening walk along the river."
    source = h.send(statement, idempotency_key="source")
    h.begin_session("return")
    h.restart_host()
    answer = h.send(
        "Do you remember the walking we discussed?",
        idempotency_key="recall",
    )
    prompt = system(model)
    assert "Prior-session user statements" in prompt
    assert statement in prompt  # english stemming: walking -> walk
    assert source.turn_id in prompt
    with psycopg.connect(dsn, autocommit=True) as conn:
        date = conn.execute(
            "SELECT created_at::date FROM conversation_turns WHERE turn_id=%s",
            (source.turn_id,),
        ).fetchone()[0].isoformat()
    assert date in prompt
    assert source.text not in prompt
    assert "USER_REPORTED" in prompt
    assert h.active_identity.conversation_id != h.sessions["source"].conversation_id
    assert answer.text != statement
    assert model.calls[-1][-1].content == "Do you remember the walking we discussed?"


def test_no_unprompted_or_irrelevant_historical_recall(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    h.send("I went walking under the moonlight.", idempotency_key="source")
    h.begin_session("return")
    h.send("What's the best pasta recipe?", idempotency_key="ordinary")
    assert "Prior-session user statements" not in system(model)
    h.send("Do you remember our discussion about gardening?",
           idempotency_key="other-topic")
    assert "Prior-session user statements" not in system(model)
    h.send("Do you remember?", idempotency_key="unspecified")
    assert "Prior-session user statements" not in system(model)
    h.send("Can you remind me?", idempotency_key="vague-reminder")
    assert "Prior-session user statements" not in system(model)
    h.send("I remember walking yesterday.", idempotency_key="statement")
    assert "Prior-session user statements" not in system(model)


def test_long_turn_excerpt_retains_late_stemmed_match_and_source(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    prefix = "The earlier details concern a long unrelated account. " * 12
    statement = prefix + "At the end I finally took an evening walk."
    assert len(prefix) > 380
    source = h.send(statement, idempotency_key="long-source")
    h.begin_session("return")
    h.send("Do you remember walking?", idempotency_key="recall")
    prompt = system(model)
    assert "Prior-session user statements" in prompt
    assert source.turn_id in prompt
    assert "evening walk" in prompt
    assert "[abridged]" in prompt
    assert prefix not in prompt
    # The headline highlight markers are internal retrieval scaffolding.
    assert "[[[" not in prompt and "]]]" not in prompt


def test_recall_prefix_words_never_become_search_topics(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    h.send(
        "I know which movie our friends recommended.",
        idempotency_key="knowledge",
    )
    h.send(
        "Please remind me which movie it was.",
        idempotency_key="reminder",
    )
    walking = h.send(
        "I started walking regularly after work.",
        idempotency_key="walking",
    )
    h.begin_session("return")
    h.send("Can you remind me?", idempotency_key="vague")
    assert "Prior-session user statements" not in system(model)
    h.send("What do you know about walking?", idempotency_key="topic")
    prompt = system(model)
    assert "Prior-session user statements" in prompt
    assert "started walking regularly" in prompt
    assert walking.turn_id in prompt
    assert "I know which movie" not in prompt
    assert "Please remind me which movie" not in prompt


def test_sensitive_raw_turn_not_promoted_to_persistent_recall_context(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    h.send(
        "I was walking near the clinic; my medical record is confidential.",
        idempotency_key="sensitive",
    )
    h.send(
        "Please keep walking API keys private: example@example.com.",
        idempotency_key="credentials",
    )
    h.begin_session("return")
    h.send("Do you remember our discussion about walking?",
           idempotency_key="recall")
    assert "Prior-session user statements" not in system(model)
    assert "medical record" not in system(model)
    assert "example@example.com" not in system(model)


def test_existing_memory_revision_outranks_raw_prior_turn(dsn):
    h, model = host(dsn)
    ident = h.begin_session("source")
    old = h.send("My favorite drink is tea.", idempotency_key="old")
    newer = h.send("My favorite drink is coffee.", idempotency_key="new")
    actor = TrustedActor(user_id=ident.user_id, source_id="mvp02b1-memory-user")
    with psycopg.connect(dsn, autocommit=True) as conn:
        admitted = admit_memory(
            conn, actor, AdmitMemoryCommand(
                instance_id=ident.instance_id, expected_generation=ident.generation,
                conversation_id=ident.conversation_id, evidence_turn_id=old.turn_id,
                evidence_quote="My favorite drink is tea.",
                memory_kind="PREFERENCE", subject_key="user",
                predicate_key="favorite_drink", value_text="tea",
                confidence=0.95, idempotency_key="admit",
            )
        )
        amended = amend_memory(
            conn, actor, AmendMemoryCommand(
                instance_id=ident.instance_id, expected_generation=ident.generation,
                conversation_id=ident.conversation_id, evidence_turn_id=newer.turn_id,
                evidence_quote="My favorite drink is coffee.",
                memory_id=admitted.memory_id, expected_version=admitted.version,
                action="CORRECT", new_value="coffee", confidence=0.95,
                idempotency_key="correct",
            )
        )
    assert amended.version == 2
    h.begin_session("return")
    h.send("Do you remember my favorite drink?", idempotency_key="recall")
    prompt = system(model)
    assert "coffee" in prompt
    assert "Prior-session user statements" not in prompt
    assert "My favorite drink is tea." not in prompt


def test_prior_turn_fallback_excludes_memory_revision_source_if_no_memory_hit(dsn):
    h, model = host(dsn)
    ident = h.begin_session("source")
    source = h.send(
        "I like walking and gardening.", idempotency_key="memory-source",
    )
    actor = TrustedActor(user_id=ident.user_id, source_id="mvp02b1-memory-user")
    with psycopg.connect(dsn, autocommit=True) as conn:
        admit_memory(
            conn, actor, AdmitMemoryCommand(
                instance_id=ident.instance_id, expected_generation=ident.generation,
                conversation_id=ident.conversation_id, evidence_turn_id=source.turn_id,
                evidence_quote="I like walking and gardening.",
                memory_kind="FACT", subject_key="user",
                predicate_key="hobby", value_text="gardening",
                confidence=0.9, idempotency_key="memory",
            ),
        )
    h.begin_session("return")
    h.send("Do you remember walking?", idempotency_key="recall")
    # The old conversation turn supplied a Memory record, so it cannot
    # bypass the Memory owner's version, privacy or correction decisions.
    prompt = system(model)
    assert "Prior-session user statements" not in prompt
    assert "I like walking and gardening." not in prompt


def test_other_user_cannot_recall_same_topic_from_another_persona(dsn):
    first, _ = host(dsn)
    first.begin_session("source")
    source = first.send(
        "I walked at the botanical garden last Saturday.",
        idempotency_key="source",
    )
    outsider, model = host(dsn)
    outsider.begin_session("return")
    outsider.send(
        "Do you remember our discussion about the botanical garden?",
        idempotency_key="recall",
    )
    assert source.turn_id not in system(model)
    assert "I walked at the botanical garden" not in system(model)


def test_prompt_budget_and_newest_user_turn_are_preserved(dsn):
    h, model = host(dsn)
    h.begin_session("source")
    for i in range(6):
        h.send(
            f"Walking practice entry {i}: " + ("walking " * 42),
            idempotency_key=f"walking-{i}",
        )
    h.begin_session("return")
    h.send("Do you remember what we said about walking?",
           idempotency_key="recall")
    prompt = model.calls[-1]
    assert sum(len(m.content) for m in prompt) <= 12000
    assert prompt[-1].role == "user"
    assert prompt[-1].content == "Do you remember what we said about walking?"
    assert system(model).count("USER_REPORTED source turn") <= 3
