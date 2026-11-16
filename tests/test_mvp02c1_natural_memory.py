"""MVP-02C.1: real-host, durable natural preference memory continuity."""
import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply


class InspectGateway:
    def __init__(self):
        self.messages=[]

    def generate(self, messages):
        self.messages.append(tuple(messages))
        return ModelReply(
            model_id="mvp02c1.inspect",
            text="I can discuss your self-reported preferences.",
        )


def test_fresh_session_retrieves_natural_preference_from_memory_owner(dsn):
    gateway=InspectGateway()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),gateway)
    host.enable_explicit_memory_worker()
    identity=host.begin_session("source")
    source=host.send("I enjoy gardening.",idempotency_key="natural-preference")
    assert host.tick_explicit_memory_worker()["admitted"]==1
    assert host.tick_explicit_memory_worker()["admitted"]==0

    # A tentative activity should not create another durable memory or Goal.
    host.send(
        "Maybe I will start walking twice next month.",
        idempotency_key="tentative-activity",
    )
    assert host.tick_explicit_memory_worker()["admitted"]==0
    state=host.observe_state()
    assert state["memories"]==1 and state["goals"]==0

    host.begin_session("recalled")
    host.restart_host()
    host.send(
        "Do you remember what I said about gardening?",
        idempotency_key="cross-session-recall",
    )
    system=gateway.messages[-1][0].content
    assert "Historical user-reported memory" in system
    assert "[PREFERENCE] user.enjoys_gardening: yes" in system
    assert source.turn_id in system
    assert gateway.messages[-1][-1].content==(
        "Do you remember what I said about gardening?"
    )
    assert host.active_identity.conversation_id!=identity.conversation_id
    assert host.tick_explicit_memory_worker()["admitted"]==0
    assert host.observe_state()["goals"]==0

    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM memory_extraction_candidates
               WHERE user_id=%s AND instance_id=%s AND status='ADMITTED'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==1


def test_stranger_cannot_recall_preference_from_another_user(dsn):
    author_model=InspectGateway()
    author=BehavioralAcceptanceHarness(
        dsn,seed_local_demo(dsn),author_model,
    )
    author.enable_explicit_memory_worker()
    author.begin_session("source")
    original=author.send(
        "I really enjoy reading.",idempotency_key="author-reading",
    )
    assert author.tick_explicit_memory_worker()["admitted"]==1

    outsider_model=InspectGateway()
    outsider=BehavioralAcceptanceHarness(
        dsn,seed_local_demo(dsn),outsider_model,
    )
    outsider.begin_session("new")
    outsider.send(
        "Do you remember what I said about reading?",
        idempotency_key="outsider-recall",
    )
    system=outsider_model.messages[-1][0].content
    assert original.turn_id not in system
    assert "user.enjoys_reading" not in system
