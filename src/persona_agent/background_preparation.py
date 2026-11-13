"""P4-01a lifecycle-owned Background epoch admission.

These helpers run *inside* Persona Runtime's short selection transaction.
They do not evaluate conversation, alter goals, or grant P2 readiness.
The authoritative autonomous admission gate remains background_ready_epochs,
which only the completed Phase 4 preparation barrier may populate.
"""

from uuid import UUID


def begin_background_epoch(
    cursor, *, user_id: UUID, instance_id: UUID, generation: int,
    background_epoch: int, presence_revision: int,
) -> None:
    """Create a unique PENDING epoch, atomically with Background transition."""
    cursor.execute(
        """
        INSERT INTO background_preparation_epochs (
            user_id, instance_id, background_epoch,
            selection_generation, presence_revision
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, instance_id, background_epoch, generation, presence_revision),
    )
    # The active-selection lock held by enter_background() serializes with
    # primary-user ingress. Capture all pre-departure conversations atomically;
    # never infer an epoch boundary from later, mutable next_sequence values.
    cursor.execute(
        """
        INSERT INTO background_preparation_conversations (
            user_id, instance_id, background_epoch,
            conversation_id, target_sequence, critical_scan_sequence
        )
        SELECT user_id, instance_id, %s, conversation_id, next_sequence - 1,
               critical_committed_watermark
        FROM conversations
        WHERE user_id = %s AND instance_id = %s
        """,
        (background_epoch, user_id, instance_id),
    )
    captured=cursor.rowcount
    cursor.execute(
        """UPDATE background_preparation_epochs
           SET captured_conversation_count=%s
           WHERE user_id=%s AND instance_id=%s AND background_epoch=%s""",
        (captured,user_id,instance_id,background_epoch),
    )


def supersede_background_epoch(
    cursor, *, user_id: UUID, instance_id: UUID, background_epoch: int,
) -> None:
    """Mark a departed epoch obsolete before foreground return/switch commits.

    Missing rows are tolerated for legacy pre-migration Background epochs.
    Epoch and generation fencing in P0 remain the authoritative worker guard.
    """
    cursor.execute(
        """
        UPDATE background_preparation_epochs
        SET status = 'SUPERSEDED', superseded_at = clock_timestamp()
        WHERE user_id = %s AND instance_id = %s AND background_epoch = %s
          AND status = 'PENDING'
        """,
        (user_id, instance_id, background_epoch),
    )
