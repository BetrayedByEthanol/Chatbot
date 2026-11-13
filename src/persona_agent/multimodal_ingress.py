"""P5-01 speech-to-text boundary reusing the single Conversation Manager.

This is not an ASR engine, a new dialogue manager, or a public transport.
The caller must authenticate the primary user and bind VoiceInputSource
independently of the provider's transcript. Partial updates remain ephemeral;
only final text uses the existing durable text-ingress command/event/outbox.
"""

from hashlib import sha256

from psycopg import Connection

from .contracts import TrustedActor
from .conversation import ingest_text
from .conversation_contracts import TextInputCommand
from .multimodal_contracts import (
    TranscriptIngressResult, TranscriptUpdate, VoiceInputProvenance,
    VoiceInputSource,
)
from .runtime import PermissionDenied


def ingest_voice_transcript(
    connection: Connection,
    actor: TrustedActor,
    source: VoiceInputSource,
    update: TranscriptUpdate,
) -> TranscriptIngressResult:
    """Finalize one authenticated utterance through conversation.ingest_text().

    Idempotency is scoped to the authenticated user/persona by the existing
    command receipt. A provider retransmission of the same final text replays
    the original turn; conflicting finalized text for the same utterance
    fails with IdempotencyConflict. Changing a captured_at value on exact
    replay does not rewrite the original evidence.

    PARTIAL deliberately performs no database access or persona activity.
    Partial hypotheses are not conversation, cognition, goal or tool input.
    The host handles any live preview, ordering or backpressure in memory.
    """
    if type(actor) is not TrustedActor:
        raise PermissionDenied("voice ingress requires authenticated primary user")
    if type(source) is not VoiceInputSource or type(update) is not TranscriptUpdate:
        raise TypeError("voice ingress requires validated host and transcript contracts")

    if update.status == "PARTIAL":
        return TranscriptIngressResult(
            utterance_id=update.utterance_id,
            disposition="PROVISIONAL",
        )

    stable_key = sha256(
        ("voice.final.v1:" + str(source.conversation_id) + ":" +
         source.channel_id + ":" + str(update.utterance_id)).encode("utf-8")
    ).hexdigest()
    committed = ingest_text(
        connection,
        actor,
        TextInputCommand(
            conversation_id=source.conversation_id,
            instance_id=source.instance_id,
            expected_generation=source.expected_generation,
            idempotency_key=stable_key,
            text=update.text,
        ),
        provenance=VoiceInputProvenance(
            utterance_id=update.utterance_id,
            channel_id=source.channel_id,
            captured_at=update.captured_at,
        ),
    )
    return TranscriptIngressResult(
        utterance_id=update.utterance_id,
        disposition="COMMITTED",
        turn_id=committed.turn_id,
        event_id=committed.event_id,
        conversation_sequence=committed.sequence,
        replayed=committed.replayed,
    )
