"""P0-02 lifecycle integration contracts against real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus

from persona_agent.contracts import RenameInstanceCommand, TrustedActor
from persona_agent.lifecycle import (
    BackgroundReason,
    EnterBackgroundCommand,
    InactivityThresholdNotReached,
    InvalidLifecycleTransition,
    LifecycleMode,
    PrimaryActivityCommand,
    SelectionResult,
    StalePresence,
    SwitchPersonaCommand,
    TrustedPlatformActor,
    enter_background,
    get_instance_mode,
    record_primary_activity,
    switch_persona,
)
from persona_agent.runtime import (
    IdempotencyConflict,
    PermissionDenied,
    StaleSelection,
    rename_instance,
)


def seed(dsn):
    user, first, second, first_definition, second_definition = (uuid4() for _ in range(5))
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.transaction():
            connection.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            connection.execute(
                "INSERT INTO persona_definitions(id, stable_key) VALUES (%s,%s),(%s,%s)",
                (
                    first_definition, f"persona-{first_definition}",
                    second_definition, f"persona-{second_definition}",
                ),
            )
            connection.execute(
                """
                INSERT INTO persona_instances(id, user_id, persona_id, label)
                VALUES (%s, %s, %s, 'One'), (%s, %s, %s, 'Two')
                """,
                (first, user, first_definition, second, user, second_definition),
            )
            connection.execute(
                """
                INSERT INTO persona_selection(user_id, instance_id, generation, mode)
                VALUES (%s, %s, 1, 'FOREGROUND')
                """,
                (user, first),
            )
    return user, first, second


def user_actor(user):
    return TrustedActor(user_id=user, source_id="authenticated-test")


def platform_actor(user):
    return TrustedPlatformActor(user_id=user, source_id="trusted-timer")


def invoke(dsn, fn, actor, command):
    with psycopg.connect(dsn, autocommit=True) as connection:
        return fn(connection, actor, command)


def switch_cmd(current, target, generation=1, key=None):
    return SwitchPersonaCommand(
        expected_instance_id=current,
        target_instance_id=target,
        expected_generation=generation,
        idempotency_key=key or str(uuid4()),
    )


def activity_cmd(instance, generation=1, key=None):
    return PrimaryActivityCommand(
        instance_id=instance, expected_generation=generation,
        idempotency_key=key or str(uuid4()),
    )


def background_cmd(instance, revision, reason=BackgroundReason.EXPLICIT_DEPARTURE, key=None):
    return EnterBackgroundCommand(
        instance_id=instance, expected_generation=1,
        expected_presence_revision=revision, reason=reason,
        idempotency_key=key or str(uuid4()),
    )


def count_events(dsn, user):
    with psycopg.connect(dsn, autocommit=True) as connection:
        return connection.execute(
            "SELECT count(*) FROM events WHERE user_id=%s", (user,),
        ).fetchone()[0]


def test_switch_changes_generation_and_derived_inactive_mode(dsn):
    user, first, second = seed(dsn)
    result = invoke(dsn, switch_persona, user_actor(user), switch_cmd(first, second))
    assert isinstance(result, SelectionResult)
    assert (result.instance_id, result.generation, result.mode) == (
        second, 2, LifecycleMode.FOREGROUND,
    )
    assert (result.background_epoch, result.presence_revision) == (1, 1)
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert get_instance_mode(connection, user_actor(user), first) == LifecycleMode.INACTIVE
        assert get_instance_mode(connection, user_actor(user), second) == LifecycleMode.FOREGROUND
        event = connection.execute(
            """
            SELECT e.event_type, e.trust_level, e.payload->>'previous_instance_id',
                   e.payload->>'selected_instance_id', count(o.outbox_id)
            FROM events e JOIN outbox o USING (event_id)
            WHERE e.event_id = %s
            GROUP BY e.event_id
            """,
            (result.event_id,),
        ).fetchone()
    assert event == (
        "persona.selection.changed.v1", "AUTHENTICATED_USER",
        str(first), str(second), 1,
    )


def test_switch_replay_and_key_collision(dsn):
    user, first, second = seed(dsn)
    cmd = switch_cmd(first, second, key="repeat-switch")
    result = invoke(dsn, switch_persona, user_actor(user), cmd)
    replayed = invoke(dsn, switch_persona, user_actor(user), cmd)
    assert replayed.replayed and replayed.event_id == result.event_id
    assert replayed.generation == 2
    assert count_events(dsn, user) == 1
    with pytest.raises(IdempotencyConflict):
        invoke(
            dsn, switch_persona, user_actor(user),
            switch_cmd(first, second, generation=2, key="repeat-switch"),
        )
    assert count_events(dsn, user) == 1


def test_unauthorized_switch_and_inactive_read_denied(dsn):
    user, first, _ = seed(dsn)
    stranger, _, stranger_second = seed(dsn)
    with pytest.raises(PermissionDenied):
        invoke(dsn, switch_persona, user_actor(user), switch_cmd(first, stranger_second))
    with psycopg.connect(dsn, autocommit=True) as connection:
        with pytest.raises(PermissionDenied):
            get_instance_mode(connection, user_actor(stranger), first)
    assert count_events(dsn, user) == 0


def test_platform_cannot_select_or_claim_user_activity(dsn):
    user, first, second = seed(dsn)
    with pytest.raises(PermissionDenied):
        invoke(dsn, switch_persona, platform_actor(user), switch_cmd(first, second))
    with pytest.raises(PermissionDenied):
        invoke(dsn, record_primary_activity, platform_actor(user), activity_cmd(first))
    assert count_events(dsn, user) == 0


def test_competing_switches_one_wins(dsn):
    user, first, second = seed(dsn)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            invoke, dsn, switch_persona, user_actor(user), switch_cmd(first, second),
        )
        future_b = executor.submit(
            invoke, dsn, switch_persona, user_actor(user), switch_cmd(first, second),
        )
        results = []
        for future in (future_a, future_b):
            try:
                results.append(future.result())
            except StaleSelection:
                results.append("stale")
    assert sum(r == "stale" for r in results) == 1
    assert count_events(dsn, user) == 1


def test_activity_returns_from_background_without_selection_generation_change(dsn):
    user, first, _ = seed(dsn)
    bg = invoke(
        dsn, enter_background, user_actor(user), background_cmd(first, revision=0),
    )
    assert (bg.mode, bg.generation, bg.background_epoch, bg.presence_revision) == (
        LifecycleMode.BACKGROUND, 1, 1, 1,
    )
    foreground = invoke(dsn, record_primary_activity, user_actor(user), activity_cmd(first))
    assert (foreground.mode, foreground.generation) == (LifecycleMode.FOREGROUND, 1)
    assert (foreground.background_epoch, foreground.presence_revision) == (2, 2)
    with pytest.raises(StalePresence):
        invoke(dsn, enter_background, platform_actor(user),
               background_cmd(first, 1, BackgroundReason.CONNECTION_CLOSED))


def test_foreground_activity_refreshes_revision_not_epoch(dsn):
    user, first, _ = seed(dsn)
    activity = invoke(dsn, record_primary_activity, user_actor(user), activity_cmd(first))
    assert (activity.generation, activity.background_epoch, activity.presence_revision) == (1, 0, 1)
    with pytest.raises(StalePresence):
        invoke(dsn, enter_background, platform_actor(user),
               background_cmd(first, 0, BackgroundReason.CONNECTION_CLOSED))
    assert count_events(dsn, user) == 1


def test_idle_timeout_requires_fifteen_minutes(dsn):
    user, first, _ = seed(dsn)
    with pytest.raises(InactivityThresholdNotReached):
        invoke(dsn, enter_background, platform_actor(user),
               background_cmd(first, 0, BackgroundReason.IDLE_TIMEOUT))
    assert count_events(dsn, user) == 0
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE persona_selection
            SET last_primary_activity_at = clock_timestamp() - interval '16 minutes'
            WHERE user_id=%s
            """,
            (user,),
        )
    result = invoke(dsn, enter_background, platform_actor(user),
                    background_cmd(first, 0, BackgroundReason.IDLE_TIMEOUT))
    assert result.mode == LifecycleMode.BACKGROUND
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert connection.execute(
            "SELECT trust_level FROM events WHERE event_id=%s", (result.event_id,),
        ).fetchone()[0] == "TRUSTED_PLATFORM"


def test_old_idle_timer_rejected_after_new_activity_even_if_aged_again(dsn):
    user, first, _ = seed(dsn)
    invoke(dsn, record_primary_activity, user_actor(user), activity_cmd(first))
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE persona_selection
            SET last_primary_activity_at = clock_timestamp() - interval '16 minutes'
            WHERE user_id=%s
            """,
            (user,),
        )
    with pytest.raises(StalePresence):
        invoke(dsn, enter_background, platform_actor(user),
               background_cmd(first, 0, BackgroundReason.IDLE_TIMEOUT))
    assert count_events(dsn, user) == 1


def test_idle_and_connection_trust_boundaries(dsn):
    user, first, _ = seed(dsn)
    with pytest.raises(PermissionDenied):
        invoke(dsn, enter_background, user_actor(user),
               background_cmd(first, 0, BackgroundReason.IDLE_TIMEOUT))
    with pytest.raises(PermissionDenied):
        invoke(dsn, enter_background, platform_actor(user),
               background_cmd(first, 0, BackgroundReason.EXPLICIT_DEPARTURE))
    assert count_events(dsn, user) == 0


def test_stale_generation_invalid_even_when_old_persona_selected_again(dsn):
    user, first, second = seed(dsn)
    invoke(dsn, switch_persona, user_actor(user), switch_cmd(first, second))
    back = invoke(dsn, switch_persona, user_actor(user), switch_cmd(second, first, 2))
    assert back.generation == 3
    with pytest.raises(StaleSelection):
        invoke(
            dsn, rename_instance, user_actor(user),
            RenameInstanceCommand(
                instance_id=first, expected_generation=1, expected_version=0,
                new_label="Unsafe stale worker", idempotency_key=str(uuid4()),
            ),
        )
    assert count_events(dsn, user) == 2


def test_activity_idempotency_cannot_reactivate_background_on_replay(dsn):
    user, first, _ = seed(dsn)
    cmd = activity_cmd(first, key="activity-once")
    first_activity = invoke(dsn, record_primary_activity, user_actor(user), cmd)
    bg = invoke(dsn, enter_background, user_actor(user),
                background_cmd(first, first_activity.presence_revision))
    assert bg.mode == LifecycleMode.BACKGROUND
    replay = invoke(dsn, record_primary_activity, user_actor(user), cmd)
    assert replay.replayed and replay.mode == LifecycleMode.FOREGROUND
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert get_instance_mode(connection, user_actor(user), first) == LifecycleMode.BACKGROUND
    assert count_events(dsn, user) == 2


def test_same_persona_switch_rolls_back_receipt(dsn):
    user, first, _ = seed(dsn)
    with pytest.raises(InvalidLifecycleTransition):
        invoke(dsn, switch_persona, user_actor(user), switch_cmd(first, first))
    assert count_events(dsn, user) == 0
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM command_receipts WHERE user_id=%s", (user,),
        ).fetchone()[0] == 0


def test_lifecycle_rejects_nested_transaction_without_any_changes(dsn):
    user, first, _ = seed(dsn)
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.transaction():
            assert connection.info.transaction_status == TransactionStatus.INTRANS
            with pytest.raises(ValueError, match="idle autocommit"):
                record_primary_activity(connection, user_actor(user), activity_cmd(first))
        with pytest.raises(ValueError, match="idle autocommit"):
            with psycopg.connect(dsn) as non_autocommit:
                record_primary_activity(non_autocommit, user_actor(user), activity_cmd(first))
    assert count_events(dsn, user) == 0


def test_background_command_rejected_for_stale_selection(dsn):
    user, first, second = seed(dsn)
    invoke(dsn, switch_persona, user_actor(user), switch_cmd(first, second))
    with pytest.raises(StaleSelection):
        invoke(dsn, enter_background, user_actor(user), background_cmd(first, 0))
    assert count_events(dsn, user) == 1
