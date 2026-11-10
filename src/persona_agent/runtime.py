"""One short, fenced PostgreSQL domain commit; no model or network calls."""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import RenameInstanceCommand, RenameInstanceResult, TrustedActor

_COMMAND_TYPE = "persona.instance.rename.v1"
_EVENT_TYPE = "persona.instance.renamed.v1"
_DOMAIN = "persona_runtime"


class PermissionDenied(Exception):
    pass


class StaleSelection(Exception):
    pass


class VersionConflict(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


def _fingerprint(command: RenameInstanceCommand) -> str:
    canonical = json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stream_id(instance_id: UUID) -> UUID:
    # Per-domain stream: later memory/goal events will not contend on this row.
    return uuid5(NAMESPACE_URL, f"persona-agent/{instance_id}/{_DOMAIN}")


def rename_instance(
    connection: Connection,
    actor: TrustedActor,
    command: RenameInstanceCommand,
) -> RenameInstanceResult:
    """Commit label, journal event, outbox and receipt in one transaction.

    The caller provides an autocommit connection so this function owns its
    transaction boundary; the actor must come from trusted authentication.
    A persona switch takes FOR UPDATE on persona_selection; this command
    takes FOR SHARE, so its generation check and commit are switch-safe.
    """
    if not connection.autocommit:
        raise ValueError("rename_instance requires an autocommit connection")

    fingerprint = _fingerprint(command)
    stream_id = _stream_id(command.instance_id)
    receipt_id = uuid4()

    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT 1 FROM persona_instances WHERE user_id = %s AND id = %s",
                (actor.user_id, command.instance_id),
            )
            if cursor.fetchone() is None:
                raise PermissionDenied("instance does not belong to authenticated user")

            cursor.execute(
                """
                INSERT INTO command_receipts (
                    receipt_id, user_id, instance_id, command_type,
                    idempotency_key, request_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, instance_id, command_type, idempotency_key)
                DO NOTHING
                RETURNING receipt_id
                """,
                (
                    receipt_id, actor.user_id, command.instance_id, _COMMAND_TYPE,
                    command.idempotency_key, fingerprint,
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    SELECT request_fingerprint, result_version, result_label, event_id
                    FROM command_receipts
                    WHERE user_id = %s AND instance_id = %s
                      AND command_type = %s AND idempotency_key = %s
                    """,
                    (actor.user_id, command.instance_id, _COMMAND_TYPE, command.idempotency_key),
                )
                saved = cursor.fetchone()
                if saved is None or saved["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("idempotency key reused with different command")
                return RenameInstanceResult(
                    event_id=saved["event_id"],
                    instance_id=command.instance_id,
                    new_version=saved["result_version"],
                    new_label=saved["result_label"],
                    replayed=True,
                )

            cursor.execute(
                """
                SELECT instance_id, generation, mode
                FROM persona_selection
                WHERE user_id = %s
                FOR SHARE
                """,
                (actor.user_id,),
            )
            selected = cursor.fetchone()
            if (
                selected is None
                or selected["instance_id"] != command.instance_id
                or selected["generation"] != command.expected_generation
                or selected["mode"] != "FOREGROUND"
            ):
                raise StaleSelection("instance is not the selected foreground generation")

            cursor.execute(
                """
                UPDATE persona_instances
                SET label = %s, version = version + 1
                WHERE id = %s AND user_id = %s AND version = %s
                RETURNING version
                """,
                (
                    command.new_label, command.instance_id, actor.user_id,
                    command.expected_version,
                ),
            )
            updated = cursor.fetchone()
            if updated is None:
                raise VersionConflict("persona instance version changed")

            cursor.execute(
                """
                INSERT INTO event_streams (stream_id, user_id, instance_id, domain)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (stream_id) DO NOTHING
                """,
                (stream_id, actor.user_id, command.instance_id, _DOMAIN),
            )
            cursor.execute(
                """
                UPDATE event_streams
                SET last_sequence = last_sequence + 1
                WHERE stream_id = %s AND user_id = %s AND instance_id = %s
                RETURNING last_sequence
                """,
                (stream_id, actor.user_id, command.instance_id),
            )
            sequence_row = cursor.fetchone()
            if sequence_row is None:
                raise PermissionDenied("invalid event stream scope")

            event_id = uuid4()
            payload = {
                "instance_id": str(command.instance_id),
                "new_label": command.new_label,
                "previous_version": command.expected_version,
                "new_version": updated["version"],
            }
            cursor.execute(
                """
                INSERT INTO events (
                    event_id, event_type, schema_version, user_id, instance_id,
                    stream_id, stream_sequence, source_id, trust_level,
                    correlation_id, payload, idempotency_key
                ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    event_id, _EVENT_TYPE, actor.user_id, command.instance_id,
                    stream_id, sequence_row["last_sequence"], actor.source_id,
                    "AUTHENTICATED_USER", receipt_id, json.dumps(payload),
                    command.idempotency_key,
                ),
            )
            cursor.execute(
                "INSERT INTO outbox (outbox_id, event_id) VALUES (%s, %s)",
                (uuid4(), event_id),
            )
            cursor.execute(
                """
                UPDATE command_receipts
                SET result_version = %s, result_label = %s, event_id = %s
                WHERE receipt_id = %s
                """,
                (updated["version"], command.new_label, event_id, receipt_id),
            )
            return RenameInstanceResult(
                event_id=event_id,
                instance_id=command.instance_id,
                new_version=updated["version"],
                new_label=command.new_label,
            )
