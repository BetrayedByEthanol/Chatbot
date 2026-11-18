"""MVP-02C.12: real PostgreSQL, authenticated source-backed shadow eligibility.

Tests exercise actual conversation turns and admitted Memory revisions. No
eligibility observation may create a Memory mutation or domain event.
"""
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.memory import admit_memory
from persona_agent.memory_contracts import AdmitMemoryCommand
from persona_agent.memory_conflict_grounding import observe_grounded_memory_eligibility
from persona_agent.runtime import PermissionDenied, StaleSelection
from persona_agent.semantic_contracts import SemanticAnnotationBatch, SemanticClaim


def _seed(dsn):
    user, definition, instance = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as db:
        with db.transaction():
            db.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            db.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                       (definition, f"mvp02c12-{definition}"))
            db.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) VALUES (%s,%s,%s,'Grounded')",
                (instance, user, definition))
            db.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')", (user, instance))
    return user, instance


def _actor(user):
    return TrustedActor(user_id=user, source_id="mvp02c12-authenticated")


def _ingest(dsn, user, instance, text, *, conversation=None):
    cid = conversation or uuid4()
    with psycopg.connect(dsn, autocommit=True) as db:
        result = ingest_text(db, _actor(user), TextInputCommand(
            instance_id=instance, conversation_id=cid,
            expected_generation=1, idempotency_key=str(uuid4()), text=text))
    return cid, result.turn_id


def _admit(dsn, user, instance, conversation, source_turn, text, *,
           predicate="enjoys_gardening", value="yes"):
    with psycopg.connect(dsn, autocommit=True) as db:
        return admit_memory(db, _actor(user), AdmitMemoryCommand(
            instance_id=instance, expected_generation=1,
            conversation_id=conversation, evidence_turn_id=source_turn,
            evidence_quote=text, memory_kind="PREFERENCE", subject_key="user",
            predicate_key=predicate, value_text=value, confidence=0.97,
            idempotency_key=str(uuid4())))


def _observe(dsn, user, instance, turn_id, **kwargs):
    with psycopg.connect(dsn, autocommit=True) as db:
        return observe_grounded_memory_eligibility(
            db, _actor(user), instance, 1, turn_id, **kwargs)


def test_real_evidence_allows_comparable_claim_and_explicit_correction_without_writes(dsn):
    user, instance = _seed(dsn)
    conversation, initial = _ingest(dsn, user, instance, "I enjoy gardening.")
    memory = _admit(dsn, user, instance, conversation, initial, "I enjoy gardening.")
    _, same = _ingest(dsn, user, instance, "I really enjoy gardening.")
    _, correction = _ingest(dsn, user, instance, "I don't enjoy gardening anymore.")

    with psycopg.connect(dsn, autocommit=True) as db:
        before = db.execute(
            "SELECT count(*) FROM events WHERE user_id=%s AND instance_id=%s",
            (user, instance)).fetchone()[0]
    first = _observe(dsn, user, instance, same)
    second = _observe(dsn, user, instance, correction)
    assert first.status == second.status == "EVALUATED"
    assert first.memory_id == second.memory_id == memory.memory_id
    assert first.memory_version == second.memory_version == 1
    assert (first.decision.disposition, first.decision.nli_allowed) == (
        "NLI_CANDIDATE", True)
    assert (second.decision.disposition, second.decision.nli_allowed) == (
        "CORRECTION_PATH", False)
    assert "value_text" not in first.__dict__
    assert "evidence_quote" not in second.__dict__
    with psycopg.connect(dsn, autocommit=True) as db:
        after = db.execute(
            "SELECT count(*) FROM events WHERE user_id=%s AND instance_id=%s",
            (user, instance)).fetchone()[0]
        record = db.execute(
            "SELECT version,value_text,record_status FROM memory_records WHERE memory_id=%s",
            (memory.memory_id,)).fetchone()
    assert before == after
    assert record == (1, "yes", "ACTIVE")


def test_no_active_slot_and_unsupported_language_abstain(dsn):
    user, instance = _seed(dsn)
    _, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    assert _observe(dsn, user, instance, turn).status == "NO_ACTIVE_MEMORY"
    _, unsupported = _ingest(dsn, user, instance, "My office is warmer than my bedroom.")
    observed = _observe(dsn, user, instance, unsupported)
    assert observed.status == "SEMANTIC_ABSTENTION"
    assert observed.decision is None
    _, past = _ingest(dsn, user, instance, "I used to enjoy gardening.")
    assert _observe(dsn, user, instance, past).status == "GRAMMAR_ABSTENTION"


def test_only_matching_slot_and_verified_current_revision_is_compared(dsn):
    user, instance = _seed(dsn)
    cid, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    memory = _admit(dsn, user, instance, cid, turn, "I enjoy gardening.")
    _, other = _ingest(dsn, user, instance, "I love reading.")
    assert _observe(dsn, user, instance, other).status == "NO_ACTIVE_MEMORY"
    _, same = _ingest(dsn, user, instance, "I enjoy gardening.")
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(
            "UPDATE memory_records SET source_quote=%s WHERE memory_id=%s",
            ("I made up a source.", memory.memory_id))
    assert _observe(dsn, user, instance, same).status == "MEMORY_EVIDENCE_ABSTENTION"


def test_retracted_or_expired_memory_not_exposed(dsn):
    user, instance = _seed(dsn)
    cid, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    memory = _admit(dsn, user, instance, cid, turn, "I enjoy gardening.")
    _, same = _ingest(dsn, user, instance, "I enjoy gardening.")
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(
            "UPDATE memory_records SET expires_at=now() - interval '1 hour' "
            "WHERE memory_id=%s", (memory.memory_id,))
    assert _observe(dsn, user, instance, same).status == "NO_ACTIVE_MEMORY"


def test_cross_user_cannot_observe_turn_or_memory(dsn):
    user, instance = _seed(dsn)
    cid, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    _admit(dsn, user, instance, cid, turn, "I enjoy gardening.")
    stranger = _actor(uuid4())
    with psycopg.connect(dsn, autocommit=True) as db:
        with pytest.raises(PermissionDenied):
            observe_grounded_memory_eligibility(db, stranger, instance, 1, turn)


def test_selection_generation_is_checked_from_db(dsn):
    user, instance = _seed(dsn)
    _, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("UPDATE persona_selection SET generation=2 WHERE user_id=%s",
                   (user,))
    with psycopg.connect(dsn, autocommit=True) as db:
        with pytest.raises(StaleSelection):
            observe_grounded_memory_eligibility(db, _actor(user), instance, 1, turn)


def test_injected_model_cannot_fabricate_correction_or_source_evidence(dsn):
    user, instance = _seed(dsn)
    cid, turn = _ingest(dsn, user, instance, "I enjoy gardening.")
    _admit(dsn, user, instance, cid, turn, "I enjoy gardening.")

    class FabricatedCorrection:
        def classify(self, text, *, source_turn_id):
            return SemanticAnnotationBatch(
                source_turn_id=source_turn_id, classifier_id="untrusted",
                classifier_version="v1", route="LLM",
                claims=(SemanticClaim(
                    evidence_quote=text, statement_type="PREFERENCE",
                    temporal_scope="CURRENT", modality="ASSERTED",
                    pragmatic_function="CORRECTING", topic_label="gardening",
                    confidence=0.9),),
            )

    got = _observe(dsn, user, instance, turn, classifier=FabricatedCorrection())
    assert got.status == "SEMANTIC_ABSTENTION"
    assert got.reason == "UNVERIFIED_SEMANTIC_LABELS"

    class HallucinatedSource(FabricatedCorrection):
        def classify(self, text, *, source_turn_id):
            output = super().classify(text, source_turn_id=source_turn_id)
            return output.model_copy(update={
                "claims": (output.claims[0].model_copy(
                    update={"evidence_quote": "I do not enjoy gardening."}),)
            })
    with pytest.raises(ValueError, match="verbatim"):
        _observe(dsn, user, instance, turn, classifier=HallucinatedSource())
