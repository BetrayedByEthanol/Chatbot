"""P4-01a: durable Background entry admission, before any autonomy is enabled."""

from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent import lifecycle
from persona_agent.contracts import TrustedActor
from persona_agent.lifecycle import (
    BackgroundReason, EnterBackgroundCommand, PrimaryActivityCommand,
    SwitchPersonaCommand, enter_background, record_primary_activity,
    switch_persona,
)
from persona_agent.operation_contracts import EnqueueOperation, Origin, Policy, Priority
from persona_agent.operation_scheduler import (
    claim_operation, configure_resource_pool, enqueue_operation,
)
from persona_agent.runtime import StaleSelection


def seed(dsn):
    user, first, second, definition1, definition2 = (uuid4() for _ in range(5))
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            conn.execute(
                """INSERT INTO persona_definitions(id, stable_key)
                   VALUES (%s, %s), (%s, %s)""",
                (definition1, f"p4-{definition1}", definition2, f"p4-{definition2}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id, user_id, persona_id, label)
                   VALUES (%s, %s, %s, 'First'), (%s, %s, %s, 'Second')""",
                (first, user, definition1, second, user, definition2),
            )
            conn.execute(
                """INSERT INTO persona_selection(user_id, instance_id, generation, mode)
                   VALUES (%s, %s, 1, 'FOREGROUND')""",
                (user, first),
            )
    return user, first, second


def actor(user):
    return TrustedActor(user_id=user, source_id="p4-primary-user")


def invoke(dsn, action, *args, **kwargs):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return action(conn, *args, **kwargs)


def depart(instance, *, generation=1, presence_revision=0, key=None):
    return EnterBackgroundCommand(
        instance_id=instance,
        expected_generation=generation,
        expected_presence_revision=presence_revision,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,
        idempotency_key=key or str(uuid4()),
    )


def epochs(conn, user):
    return conn.execute(
        """SELECT instance_id, background_epoch, selection_generation,
                  presence_revision, status, superseded_at
           FROM background_preparation_epochs
           WHERE user_id=%s ORDER BY background_epoch""",
        (user,),
    ).fetchall()


def test_entry_creates_pending_epoch_in_lifecycle_transaction(dsn):
    user, first, _ = seed(dsn)
    entered = invoke(dsn, enter_background, actor(user), depart(first))
    assert (entered.generation, entered.background_epoch, entered.presence_revision) == (1, 1, 1)

    with psycopg.connect(dsn, autocommit=True) as conn:
        assert epochs(conn, user) == [(first, 1, 1, 1, "PENDING", None)]
        assert conn.execute(
            """SELECT count(*) FROM background_ready_epochs
               WHERE user_id=%s AND instance_id=%s AND background_epoch=1""",
            (user, first),
        ).fetchone()[0] == 0
        event = conn.execute(
            """SELECT e.event_type, e.payload->>'background_epoch',
                      count(o.outbox_id)
               FROM events e JOIN outbox o ON o.event_id=e.event_id
               WHERE e.event_id=%s GROUP BY e.event_id""",
            (entered.event_id,),
        ).fetchone()
        assert event == ("persona.mode.changed.v1", "1", 1)


def test_entry_idempotency_does_not_create_another_epoch(dsn):
    user, first, _ = seed(dsn)
    command = depart(first, key="repeat-p4-entry")
    first_result = invoke(dsn, enter_background, actor(user), command)
    again = invoke(dsn, enter_background, actor(user), command)
    assert again.replayed is True
    assert again.event_id == first_result.event_id
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert len(epochs(conn, user)) == 1


def test_return_supersedes_old_epoch_and_next_departure_is_new(dsn):
    user, first, _ = seed(dsn)
    invoke(dsn, enter_background, actor(user), depart(first))
    returned = invoke(dsn, record_primary_activity, actor(user), PrimaryActivityCommand(
        instance_id=first, expected_generation=1, idempotency_key=str(uuid4()),
    ))
    assert (returned.background_epoch, returned.presence_revision) == (2, 2)
    next_entry = invoke(dsn, enter_background, actor(user),
                        depart(first, presence_revision=2))
    assert next_entry.background_epoch == 3
    with psycopg.connect(dsn, autocommit=True) as conn:
        records = epochs(conn, user)
        assert [(r[1], r[4]) for r in records] == [(1, "SUPERSEDED"), (3, "PENDING")]
        assert records[0][5] is not None and records[1][5] is None


def test_persona_switch_supersedes_without_starting_unselected_preparation(dsn):
    user, first, second = seed(dsn)
    invoke(dsn, enter_background, actor(user), depart(first))
    switched = invoke(dsn, switch_persona, actor(user), SwitchPersonaCommand(
        expected_instance_id=first, target_instance_id=second,
        expected_generation=1, idempotency_key=str(uuid4()),
    ))
    assert (switched.generation, switched.background_epoch) == (2, 2)
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = epochs(conn, user)
        assert len(rows) == 1 and rows[0][0] == first and rows[0][4] == "SUPERSEDED"
    with pytest.raises(StaleSelection):
        invoke(dsn, enter_background, actor(user), depart(first, generation=1))


def test_failure_after_epoch_insert_rolls_back_lifecycle_and_outbox(dsn, monkeypatch):
    user, first, _ = seed(dsn)
    insert = lifecycle.begin_background_epoch

    def fail_after_insert(cursor, **kwargs):
        insert(cursor, **kwargs)
        raise RuntimeError("simulated transaction interruption")

    monkeypatch.setattr(lifecycle, "begin_background_epoch", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated"):
        invoke(dsn, enter_background, actor(user), depart(first))
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert epochs(conn, user) == []
        assert conn.execute(
            """SELECT mode, background_epoch, presence_revision
               FROM persona_selection WHERE user_id=%s""",
            (user,),
        ).fetchone() == ("FOREGROUND", 0, 0)
        assert conn.execute(
            "SELECT count(*) FROM events WHERE user_id=%s", (user,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM command_receipts WHERE user_id=%s", (user,),
        ).fetchone()[0] == 0


def test_pending_epoch_does_not_grant_p2_admission(dsn):
    user, first, _ = seed(dsn)
    key = f"p4-resource-{uuid4()}"
    operation_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        configure_resource_pool(conn, key, capacity_units=1)
        enqueue_operation(conn, actor(user), EnqueueOperation(
            operation_id=operation_id,
            instance_id=first, expected_generation=1,
            idempotency_key=f"p4-{operation_id}",
            operation_type="test.p4-epoch-admission",
            origin=Origin.USER,
            execution_policy=Policy.BACKGROUND_RESUMABLE,
            priority_class=Priority.P2,
            resources={key: 1},
        ))
    invoke(dsn, enter_background, actor(user), depart(first))
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert claim_operation(
            conn, worker_id="p4-worker",
            operation_types=("test.p4-epoch-admission",),
        ) is None
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()[0] == "QUEUED"
