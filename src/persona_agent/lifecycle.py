"""Persona selection and lifecycle transitions (v2.5 P0-02).

Only this module may mutate persona_selection. All write commands own a short
PostgreSQL transaction and use its row as the authority serialization gate.
"""

from datetime import timedelta
from enum import Enum
import json
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .background_preparation import begin_background_epoch, supersede_background_epoch
from .runtime import (
    IdempotencyConflict,
    PermissionDenied,
    StaleSelection,
    _fingerprint,
    _stream_id,
)


class TrustedPlatformActor(BaseModel):
    """Only trusted platform ingress may construct platform-initiated actors."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: UUID
    source_id: str = Field(min_length=1, max_length=128)


class LifecycleMode(str, Enum):
    FOREGROUND = "FOREGROUND"
    BACKGROUND = "BACKGROUND"
    INACTIVE = "INACTIVE"  # Derived from not being the selected instance.


class BackgroundReason(str, Enum):
    EXPLICIT_DEPARTURE = "EXPLICIT_DEPARTURE"
    CONNECTION_CLOSED = "CONNECTION_CLOSED"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"


class SwitchPersonaCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_instance_id: UUID
    target_instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class PrimaryActivityCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class EnterBackgroundCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    expected_presence_revision: int = Field(ge=0)
    reason: BackgroundReason
    idempotency_key: str = Field(min_length=1, max_length=128)


class SelectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: UUID
    instance_id: UUID
    generation: int
    mode: LifecycleMode
    background_epoch: int
    presence_revision: int
    replayed: bool = False


class InvalidLifecycleTransition(Exception):
    pass


class StalePresence(Exception):
    pass


class InactivityThresholdNotReached(Exception):
    pass


def _ensure_boundary(connection: Connection) -> None:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("lifecycle commands require an idle autocommit connection")


def _check_owner(cursor, user_id: UUID, instance_id: UUID) -> None:
    cursor.execute(
        "SELECT 1 FROM persona_instances WHERE user_id = %s AND id = %s",
        (user_id, instance_id),
    )
    if cursor.fetchone() is None:
        raise PermissionDenied("instance does not belong to authenticated scope")


def _execute(
    connection: Connection,
    actor: TrustedActor | TrustedPlatformActor,
    command: SwitchPersonaCommand | PrimaryActivityCommand | EnterBackgroundCommand,
    *,
    target_id: UUID,
    command_type: str,
    event_type: str,
    transition,
) -> SelectionResult:
    _ensure_boundary(connection)
    fingerprint = _fingerprint(command)
    receipt_id = uuid4()

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            _check_owner(cursor, actor.user_id, target_id)
            cursor.execute(
                """
                INSERT INTO command_receipts (
                    receipt_id, user_id, instance_id, command_type,
                    idempotency_key, request_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, instance_id, command_type, idempotency_key)
                DO NOTHING RETURNING receipt_id
                """,
                (
                    receipt_id, actor.user_id, target_id, command_type,
                    command.idempotency_key, fingerprint,
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    SELECT request_fingerprint, result_json FROM command_receipts
                    WHERE user_id = %s AND instance_id = %s
                        AND command_type = %s AND idempotency_key = %s
                    """,
                    (actor.user_id, target_id, command_type, command.idempotency_key),
                )
                saved = cursor.fetchone()
                if saved is None or saved["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("idempotency key reused with different command")
                if saved["result_json"] is None:
                    raise RuntimeError("committed lifecycle command missing result")
                return SelectionResult.model_validate(saved["result_json"]).model_copy(update={"replayed": True})

            # Lock in same order as P0-01 rename's shared authority gate.
            cursor.execute(
                """
                SELECT instance_id, generation, mode, background_epoch,
                       presence_revision, last_primary_activity_at,
                       now() AS server_now
                FROM persona_selection
                WHERE user_id = %s
                FOR UPDATE
                """,
                (actor.user_id,),
            )
            current = cursor.fetchone()
            if current is None or current["generation"] != command.expected_generation:
                raise StaleSelection("selected generation has changed")

            updated, payload = transition(cursor, current)
            stream_id = _stream_id(target_id)
            cursor.execute(
                """
                INSERT INTO event_streams (stream_id, user_id, instance_id, domain)
                VALUES (%s, %s, %s, 'persona_runtime')
                ON CONFLICT (stream_id) DO NOTHING
                """,
                (stream_id, actor.user_id, target_id),
            )
            cursor.execute(
                """
                UPDATE event_streams SET last_sequence = last_sequence + 1
                WHERE stream_id = %s AND user_id = %s AND instance_id = %s
                RETURNING last_sequence
                """,
                (stream_id, actor.user_id, target_id),
            )
            stream_sequence = cursor.fetchone()
            if stream_sequence is None:
                raise PermissionDenied("event stream belongs to different instance")

            event_id = uuid4()
            cursor.execute(
                """
                INSERT INTO events (
                    event_id, event_type, schema_version, user_id, instance_id,
                    stream_id, stream_sequence, source_id, trust_level,
                    correlation_id, payload, idempotency_key
                ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    event_id, event_type, actor.user_id, target_id,
                    stream_id, stream_sequence["last_sequence"], actor.source_id,
                    "TRUSTED_PLATFORM" if isinstance(actor, TrustedPlatformActor) else "AUTHENTICATED_USER",
                    receipt_id, json.dumps(payload), command.idempotency_key,
                ),
            )
            cursor.execute(
                "INSERT INTO outbox(outbox_id, event_id) VALUES (%s, %s)",
                (uuid4(), event_id),
            )
            result = SelectionResult(
                event_id=event_id,
                instance_id=updated["instance_id"],
                generation=updated["generation"],
                mode=updated["mode"],
                background_epoch=updated["background_epoch"],
                presence_revision=updated["presence_revision"],
            )
            cursor.execute(
                """
                UPDATE command_receipts
                SET result_json = %s::jsonb, event_id = %s
                WHERE receipt_id = %s
                """,
                (result.model_dump_json(), event_id, receipt_id),
            )
            return result


def switch_persona(
    connection: Connection,
    actor: TrustedActor,
    command: SwitchPersonaCommand,
) -> SelectionResult:
    """Select another owned persona. Old generation revoked at commit."""
    if type(actor) is not TrustedActor:
        raise PermissionDenied("persona selection requires authenticated primary user")

    def transition(cursor, current):
        if current["instance_id"] != command.expected_instance_id:
            raise StaleSelection("different selected instance")
        if current["instance_id"] == command.target_instance_id:
            raise InvalidLifecycleTransition("already selected; use primary activity for return")
        if current["mode"] == "BACKGROUND":
            supersede_background_epoch(
                cursor, user_id=actor.user_id,
                instance_id=current["instance_id"],
                background_epoch=current["background_epoch"],
            )
        cursor.execute(
            """
            UPDATE persona_selection
            SET instance_id = %s, mode = 'FOREGROUND',
                generation = generation + 1,
                background_epoch = background_epoch + 1,
                presence_revision = presence_revision + 1,
                last_primary_activity_at = clock_timestamp()
            WHERE user_id = %s
            RETURNING instance_id, generation, mode, background_epoch, presence_revision
            """,
            (command.target_instance_id, actor.user_id),
        )
        updated = cursor.fetchone()
        return updated, {
            "previous_instance_id": str(current["instance_id"]),
            "selected_instance_id": str(updated["instance_id"]),
            "previous_generation": current["generation"],
            "generation": updated["generation"],
            "background_epoch": updated["background_epoch"],
            "presence_revision": updated["presence_revision"],
            "mode": updated["mode"],
        }

    return _execute(
        connection, actor, command, target_id=command.target_instance_id,
        command_type="persona.selection.switch.v1",
        event_type="persona.selection.changed.v1",
        transition=transition,
    )


def record_primary_activity(
    connection: Connection,
    actor: TrustedActor,
    command: PrimaryActivityCommand,
) -> SelectionResult:
    """Authenticated activity returns to Foreground and invalidates old idle timers."""
    if type(actor) is not TrustedActor:
        raise PermissionDenied("primary activity requires authenticated primary user")

    def transition(cursor, current):
        if current["instance_id"] != command.instance_id:
            raise StaleSelection("activity targeted an inactive instance")
        if current["mode"] == "BACKGROUND":
            supersede_background_epoch(
                cursor, user_id=actor.user_id,
                instance_id=command.instance_id,
                background_epoch=current["background_epoch"],
            )
        cursor.execute(
            """
            UPDATE persona_selection
            SET mode = 'FOREGROUND',
                background_epoch = background_epoch +
                    CASE WHEN mode = 'BACKGROUND' THEN 1 ELSE 0 END,
                presence_revision = presence_revision + 1,
                last_primary_activity_at = clock_timestamp()
            WHERE user_id = %s
            RETURNING instance_id, generation, mode, background_epoch, presence_revision
            """,
            (actor.user_id,),
        )
        updated = cursor.fetchone()
        return updated, {
            "instance_id": str(command.instance_id),
            "previous_mode": current["mode"],
            "mode": updated["mode"],
            "generation": updated["generation"],
            "background_epoch": updated["background_epoch"],
            "presence_revision": updated["presence_revision"],
        }

    return _execute(
        connection, actor, command, target_id=command.instance_id,
        command_type="persona.presence.primary_activity.v1",
        event_type="persona.presence.observed.v1",
        transition=transition,
    )


def enter_background(
    connection: Connection,
    actor: TrustedActor | TrustedPlatformActor,
    command: EnterBackgroundCommand,
) -> SelectionResult:
    """Explicit departure/connection close or guarded 15-minute idle expiry."""
    if command.reason == BackgroundReason.EXPLICIT_DEPARTURE:
        if type(actor) is not TrustedActor:
            raise PermissionDenied("departure requires authenticated primary user")
    elif type(actor) is not TrustedPlatformActor:
        raise PermissionDenied("connection/idle callbacks require trusted platform")

    def transition(cursor, current):
        if current["instance_id"] != command.instance_id:
            raise StaleSelection("background request targeted inactive instance")
        if current["presence_revision"] != command.expected_presence_revision:
            raise StalePresence("presence changed since callback was created")
        if current["mode"] != "FOREGROUND":
            raise InvalidLifecycleTransition("selected instance is not Foreground")
        if command.reason == BackgroundReason.IDLE_TIMEOUT and (
            current["server_now"] - current["last_primary_activity_at"] < timedelta(minutes=15)
        ):
            raise InactivityThresholdNotReached("authenticated inactivity below 15 minutes")

        cursor.execute(
            """
            UPDATE persona_selection
            SET mode = 'BACKGROUND', background_epoch = background_epoch + 1,
                presence_revision = presence_revision + 1
            WHERE user_id = %s
            RETURNING instance_id, generation, mode, background_epoch, presence_revision
            """,
            (actor.user_id,),
        )
        updated = cursor.fetchone()
        begin_background_epoch(
            cursor, user_id=actor.user_id,
            instance_id=updated["instance_id"],
            generation=updated["generation"],
            background_epoch=updated["background_epoch"],
            presence_revision=updated["presence_revision"],
        )
        return updated, {
            "instance_id": str(command.instance_id),
            "previous_mode": current["mode"],
            "mode": updated["mode"],
            "reason": command.reason.value,
            "generation": updated["generation"],
            "background_epoch": updated["background_epoch"],
            "presence_revision": updated["presence_revision"],
        }

    return _execute(
        connection, actor, command, target_id=command.instance_id,
        command_type="persona.presence.enter_background.v1",
        event_type="persona.mode.changed.v1",
        transition=transition,
    )


def get_instance_mode(
    connection: Connection,
    actor: TrustedActor,
    instance_id: UUID,
) -> LifecycleMode:
    """Read authorized lifecycle; unselected persona instances are Inactive."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT CASE WHEN s.instance_id = i.id THEN s.mode
                        ELSE 'INACTIVE' END AS instance_mode
            FROM persona_instances AS i
            LEFT JOIN persona_selection AS s ON s.user_id = i.user_id
            WHERE i.id = %s AND i.user_id = %s
            """,
            (instance_id, actor.user_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise PermissionDenied("instance does not belong to authenticated user")
        return LifecycleMode(row["instance_mode"])
