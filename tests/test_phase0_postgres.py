"""Real PostgreSQL integration tests; TEST_DATABASE_URL enables local execution."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.contracts import RenameInstanceCommand, TrustedActor
from persona_agent.runtime import (
    IdempotencyConflict,
    PermissionDenied,
    StaleSelection,
    VersionConflict,
    rename_instance,
)


@pytest.fixture(scope="session")
def dsn():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "0001_phase0_core.sql").read_text()
    with psycopg.connect(value, autocommit=True) as connection:
        connection.execute(sql)
    return value


def seed(dsn, user_id=None):
    user_id = user_id or uuid4()
    persona_id, alternative_id = uuid4(), uuid4()
    instance_id, other_instance_id = uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.transaction():
            connection.execute("INSERT INTO users(id) VALUES (%s)", (user_id,))
            connection.execute(
                "INSERT INTO persona_definitions(id, stable_key) VALUES (%s, %s), (%s, %s)",
                (persona_id, f"persona-{persona_id}", alternative_id, f"persona-{alternative_id}"),
            )
            connection.execute(
                """
                INSERT INTO persona_instances(id, user_id, persona_id, label)
                VALUES (%s, %s, %s, 'Original'), (%s, %s, %s, 'Alternative')
                """,
                (instance_id, user_id, persona_id, other_instance_id, user_id, alternative_id),
            )
            connection.execute(
                """
                INSERT INTO persona_selection(user_id, instance_id, generation, mode)
                VALUES (%s, %s, 1, 'FOREGROUND')
                """,
                (user_id, instance_id),
            )
    return user_id, instance_id, other_instance_id


def command(instance_id, key, *, version=0, generation=1, label="Friendly"):
    return RenameInstanceCommand(
        instance_id=instance_id, expected_generation=generation,
        expected_version=version, new_label=label, idempotency_key=key,
    )


def invoke(dsn, user_id, cmd):
    with psycopg.connect(dsn, autocommit=True) as connection:
        return rename_instance(connection, TrustedActor(user_id=user_id, source_id="test"), cmd)


def count(dsn, table, instance_id):
    with psycopg.connect(dsn, autocommit=True) as connection:
        if table == "outbox":
            return connection.execute(
                "SELECT count(*) FROM outbox o JOIN events e USING (event_id) WHERE e.instance_id=%s",
                (instance_id,),
            ).fetchone()[0]
        return connection.execute(
            "SELECT count(*) FROM events WHERE instance_id=%s", (instance_id,),
        ).fetchone()[0]


def test_atomic_command_idempotency_and_replay(dsn):
    user, instance, _ = seed(dsn)
    cmd = command(instance, str(uuid4()))
    first = invoke(dsn, user, cmd)
    repeated = invoke(dsn, user, cmd)
    assert first.new_version == 1 and not first.replayed
    assert repeated.replayed and repeated.event_id == first.event_id
    assert count(dsn, "events", instance) == count(dsn, "outbox", instance) == 1
    with psycopg.connect(dsn, autocommit=True) as connection:
        event = connection.execute(
            "SELECT stream_sequence, trust_level, payload->>'new_label' FROM events WHERE event_id=%s",
            (first.event_id,),
        ).fetchone()
    assert event == (1, "AUTHENTICATED_USER", "Friendly")


def test_key_collision_is_rejected(dsn):
    user, instance, _ = seed(dsn)
    key = str(uuid4())
    invoke(dsn, user, command(instance, key))
    with pytest.raises(IdempotencyConflict):
        invoke(dsn, user, command(instance, key, label="Other"))
    assert count(dsn, "events", instance) == 1


def test_conflicting_version_rolls_back_all_effects(dsn):
    user, instance, _ = seed(dsn)
    invoke(dsn, user, command(instance, "first"))
    with pytest.raises(VersionConflict):
        invoke(dsn, user, command(instance, "second", label="Conflict"))
    assert count(dsn, "events", instance) == count(dsn, "outbox", instance) == 1


def test_scope_denial_creates_no_receipt_or_event(dsn):
    owner, instance, _ = seed(dsn)
    stranger, _, _ = seed(dsn)
    with pytest.raises(PermissionDenied):
        invoke(dsn, stranger, command(instance, "unauthorized"))
    assert count(dsn, "events", instance) == 0
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM command_receipts WHERE instance_id=%s", (instance,),
        ).fetchone()[0] == 0


def test_stale_generation_after_persona_switch(dsn):
    user, instance, alternate = seed(dsn)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE persona_selection
            SET instance_id=%s, generation=generation+1
            WHERE user_id=%s
            """,
            (alternate, user),
        )
    with pytest.raises(StaleSelection):
        invoke(dsn, user, command(instance, "stale-generation"))
    assert count(dsn, "events", instance) == 0


def test_ordered_stream_replay(dsn):
    user, instance, _ = seed(dsn)
    invoke(dsn, user, command(instance, "first", label="First"))
    invoke(dsn, user, command(instance, "second", version=1, label="Second"))
    with psycopg.connect(dsn, autocommit=True) as connection:
        rows = connection.execute(
            """
            SELECT stream_sequence, payload->>'new_label' FROM events
            WHERE instance_id=%s ORDER BY stream_sequence
            """,
            (instance,),
        ).fetchall()
    assert rows == [(1, "First"), (2, "Second")]


def test_competing_writers_one_version_wins(dsn):
    user, instance, _ = seed(dsn)
    cmds = [command(instance, f"racer-{uuid4()}", label=label) for label in ("A", "B")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, dsn, user, cmd) for cmd in cmds]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except VersionConflict:
                outcomes.append("conflict")
    assert sum(result == "conflict" for result in outcomes) == 1
    assert count(dsn, "events", instance) == 1


def test_independent_user_instances_both_commit(dsn):
    user_a, instance_a, _ = seed(dsn)
    user_b, instance_b, _ = seed(dsn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(invoke, dsn, user_a, command(instance_a, "a"))
        b = pool.submit(invoke, dsn, user_b, command(instance_b, "b"))
        assert a.result().new_version == b.result().new_version == 1
    assert count(dsn, "events", instance_a) == count(dsn, "events", instance_b) == 1
