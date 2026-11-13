"""P5-01: voice finalization uses Phase 1 conversation ownership unchanged."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

psycopg = pytest.importorskip("psycopg")

from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, prepare_text_context, respond_to_text,
)
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.multimodal_contracts import (
    TranscriptUpdate, VoiceInputSource,
)
from persona_agent.multimodal_ingress import ingest_voice_transcript
from persona_agent.runtime import IdempotencyConflict, PermissionDenied, StaleSelection
from test_phase1_conversation import FakeGateway, actor, call, seed


def source(instance, conversation=None, channel="voice:local", generation=1):
    return VoiceInputSource(
        instance_id=instance,
        conversation_id=conversation or uuid4(),
        expected_generation=generation,
        channel_id=channel,
    )


def transcript(text="Hello aloud", status="FINAL", utterance_id=None,
               captured_at=None, revision=1):
    return TranscriptUpdate(
        utterance_id=utterance_id or uuid4(),
        revision=revision,
        status=status,
        text=text,
        captured_at=captured_at or datetime.now(timezone.utc),
    )


def test_partial_updates_do_not_access_database_or_create_conversation_state():
    user, instance = uuid4(), uuid4()
    src = source(instance)
    first = transcript("hel", status="PARTIAL")
    result = ingest_voice_transcript(None, actor(user), src, first)
    assert result.disposition == "PROVISIONAL"
    assert result.turn_id is None and result.event_id is None
    assert result.conversation_sequence is None
    later = first.model_copy(update={"revision": 2, "text": "hello"})
    assert ingest_voice_transcript(None, actor(user), src, later).turn_id is None
    with pytest.raises(PermissionDenied):
        ingest_voice_transcript(None, object(), src, later)


def test_transcript_validation_rejects_bad_final_and_naive_capture():
    with pytest.raises(ValidationError):
        transcript("   ")
    with pytest.raises(ValidationError):
        transcript("hello", captured_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        TranscriptUpdate(
            utterance_id=uuid4(), revision=0, status="FINAL",
            text="hello", captured_at=datetime.now(timezone.utc),
            injected_selection_generation=999,
        )


def test_final_transcript_commits_one_turn_with_source_provenance(dsn):
    user, instance = seed(dsn)
    src = source(instance)
    captured = datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc)
    update = transcript("  Hello aloud  ", captured_at=captured)
    first = call(dsn, ingest_voice_transcript, actor(user), src, update)
    second = call(dsn, ingest_voice_transcript, actor(user), src, update)
    assert first.disposition == "COMMITTED"
    assert not first.replayed and second.replayed
    assert first.turn_id == second.turn_id and first.event_id == second.event_id
    assert first.conversation_sequence == 1
    with psycopg.connect(dsn, autocommit=True) as conn:
        turn = conn.execute(
            "SELECT source_id,input_text FROM conversation_turns WHERE turn_id=%s",
            (first.turn_id,),
        ).fetchone()
        assert turn == ("phase1-authenticated", "Hello aloud")
        record = conn.execute(
            "SELECT event_type,trust_level,payload FROM events WHERE event_id=%s",
            (first.event_id,),
        ).fetchone()
        assert record[0] == "conversation.user_text.received.v1"
        assert record[1] == "AUTHENTICATED_USER"
        assert record[2]["input_modality"] == "VOICE"
        assert record[2]["utterance_id"] == str(update.utterance_id)
        assert record[2]["channel_id"] == src.channel_id
        assert record[2]["captured_at"] == captured.isoformat()
        assert record[2]["capture_time_basis"] == "SOURCE_REPORTED"
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",
            (first.event_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (src.conversation_id,),
        ).fetchone()[0] == 1


def test_voice_and_text_share_history_model_and_conversation_revision(dsn):
    user, instance = seed(dsn)
    conv = uuid4()
    src = source(instance, conv)
    spoken = call(dsn, ingest_voice_transcript, actor(user), src, transcript())
    reply = call(dsn, respond_to_text, actor(user), spoken.turn_id,
                 FakeGateway("I heard you"))
    call(dsn, acknowledge_text_delivery, actor(user), reply.response_id)
    typed = call(
        dsn, ingest_text, actor(user),
        TextInputCommand(
            conversation_id=conv, instance_id=instance, expected_generation=1,
            idempotency_key="typed-follow-up", text="What did I say?",
        ),
    )
    context = call(dsn, prepare_text_context, actor(user), typed.turn_id)
    assert [(m.role, m.content) for m in context.messages[1:]] == [
        ("user", "Hello aloud"),
        ("assistant", "I heard you"),
        ("user", "What did I say?"),
    ]
    assert spoken.conversation_sequence == 1 and typed.sequence == 2
    assert call(dsn, respond_to_text, actor(user), typed.turn_id,
                FakeGateway("You said hello")).text == "You said hello"


def test_conflicting_final_and_cross_channel_identity(dsn):
    user, instance = seed(dsn)
    conv = uuid4()
    src = source(instance, conv)
    first_update = transcript()
    call(dsn, ingest_voice_transcript, actor(user), src, first_update)
    with pytest.raises(IdempotencyConflict):
        call(
            dsn, ingest_voice_transcript, actor(user), src,
            first_update.model_copy(update={"text": "different final"}),
        )
    other_channel = source(instance, conv, channel="voice:second")
    next_turn = call(
        dsn, ingest_voice_transcript, actor(user), other_channel, first_update,
    )
    assert next_turn.conversation_sequence == 2


def test_no_authority_from_transcript_user_or_inactive_persona(dsn):
    user, first, other = seed(dsn, two=True)
    stranger, _ = seed(dsn)
    src = source(first)
    final = transcript()
    with pytest.raises(PermissionDenied):
        call(dsn, ingest_voice_transcript, actor(stranger), src, final)
    call(
        dsn, switch_persona, actor(user),
        SwitchPersonaCommand(
            expected_instance_id=first, target_instance_id=other,
            expected_generation=1, idempotency_key="switch-before-voice",
        ),
    )
    with pytest.raises(StaleSelection):
        call(dsn, ingest_voice_transcript, actor(user), src, final)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (src.conversation_id,),
        ).fetchone()[0] == 0


def test_concurrent_duplicate_finals_single_commit_and_late_partial(dsn):
    user, instance = seed(dsn)
    src = source(instance)
    update = transcript()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(call, dsn, ingest_voice_transcript, actor(user), src, update)
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert results[0].turn_id == results[1].turn_id
    assert sum(not x.replayed for x in results) == 1
    late_partial = update.model_copy(update={"status": "PARTIAL", "text": "outdated"})
    assert ingest_voice_transcript(None, actor(user), src, late_partial).turn_id is None
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (src.conversation_id,),
        ).fetchone()[0] == 1
