"""Fixture helpers for exact, owner-linked Memory source invalidation."""
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from persona_agent.memory import admit_memory, amend_memory
from test_phase2_memory import actor, call, candidate, correction, turn


def link_memory(dsn,user,instance,conversation,turn_id):
    # Deliberately attach a record to the exact workflow evidence turn;
    # whole-turn invalidation must not depend on textual similarity.
    with psycopg.connect(dsn,autocommit=True) as conn:
        quote=conn.execute("SELECT input_text FROM conversation_turns WHERE turn_id=%s",
                           (turn_id,)).fetchone()[0]
    return call(dsn,admit_memory,actor(user),candidate(
        instance,conversation,SimpleNamespace(turn_id=turn_id),
        kind="FACT",predicate="workflow_evidence",value="reported",quote=quote,
    ))


def invalidate(dsn,user,instance,saved,change="CORRECT"):
    if change=="EXPIRE":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_records SET expires_at=clock_timestamp()-interval '1 second' WHERE memory_id=%s",
                         (saved.memory_id,))
        return
    conv=uuid4()
    quote="Actually that earlier report was mistaken."
    newer=turn(dsn,user,instance,conv,quote)
    return call(dsn,amend_memory,actor(user),correction(
        instance,conv,newer,saved.memory_id,action=change,value="corrected",quote=quote,
    ))
