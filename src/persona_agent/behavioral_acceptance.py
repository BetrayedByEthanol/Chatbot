"""MVP-00A: observational, multi-session behavioral acceptance over the real host.

Only identity provisioning is a fixture. All behavior enters via
LocalAcceptanceSession.message(). Workers use existing domain APIs; observation
uses a PostgreSQL read-only transaction and cannot fabricate domain success.
"""
from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .acceptance_host import HostIdentity, LocalAcceptanceSession
from .memory_extraction import (
    register_extraction_consumer, run_extraction_tick,
)


@dataclass(frozen=True)
class ObservedTurn:
    session: str
    turn_id: str
    response_id: str
    text: str
    model_id: str
    replayed: bool


# These are authoritative domain tables, not projections inferred from text.
_SCOPED_TABLES = {
    "conversations": "conversations",
    "turns": "conversation_turns",
    "memories": "memory_records",
    "goals": "goal_records",
    "goal_evidence": "goal_evidence",
    "reflections": "cognitive_reflections",
    "intentions": "cognitive_intentions",
    "emotion_appraisals": "emotion_appraisals",
    "relationship_observations": "relationship_observations",
}


class BehavioralAcceptanceHarness:
    """Drive ordinary turns across durable sessions of one selected persona.

    Do not add goal/memory/cognition write helpers here. If a conversation
    cannot cause a desired change, that is an acceptance gap, not a fixture.
    """

    def __init__(
        self,
        dsn: str,
        identity: HostIdentity,
        gateway,
        deliver: Callable[[dict], None] | None = None,
    ):
        self.dsn = dsn
        self.identity = identity
        self.gateway = gateway
        self.deliveries: list[dict] = []
        self._deliver = deliver if deliver is not None else self.deliveries.append
        self.sessions: dict[str, HostIdentity] = {}
        self.turns: list[ObservedTurn] = []
        self._session_name: str | None = None
        self._host: LocalAcceptanceSession | None = None

    @property
    def active_identity(self) -> HostIdentity:
        if self._session_name is None:
            raise RuntimeError("start a session before sending messages")
        return self.sessions[self._session_name]

    def begin_session(self, name: str) -> HostIdentity:
        """A new conversation, NOT a new user or persona instance."""
        if not name or name in self.sessions:
            raise ValueError("session name must be new and nonempty")
        conversation_id = (
            self.identity.conversation_id if not self.sessions else uuid4()
        )
        identity = HostIdentity(
            self.identity.user_id,
            self.identity.instance_id,
            conversation_id,
            self.identity.generation,
        )
        self.sessions[name] = identity
        self.resume_session(name)
        return identity

    def resume_session(self, name: str) -> None:
        """Rebuild the transport from existing persisted identity and state."""
        if name not in self.sessions:
            raise ValueError("unknown session")
        self._session_name = name
        self._host = LocalAcceptanceSession(
            self.dsn, self.sessions[name], self.gateway, self._deliver,
        )

    def restart_host(self) -> None:
        """Simulate transport/process reinitialization without rewriting state."""
        if self._session_name is None:
            raise RuntimeError("no active session")
        self.resume_session(self._session_name)

    def send(
        self,
        text: str,
        *,
        idempotency_key: str,
        mode: str = "model",
    ) -> ObservedTurn:
        """Send input through existing authenticated conversation ingress."""
        if self._host is None or self._session_name is None:
            raise RuntimeError("no active session")
        response = self._host.message(
            text=text, idempotency_key=idempotency_key, mode=mode,
        )
        observed = ObservedTurn(
            session=self._session_name,
            turn_id=response["turn_id"],
            response_id=response["response_id"],
            text=response["text"],
            model_id=response["model_id"],
            replayed=bool(response["replayed"]),
        )
        self.turns.append(observed)
        return observed

    def enable_explicit_memory_worker(self) -> None:
        """Subscribe the already-implemented extraction consumer."""
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            register_extraction_consumer(conn)

    def tick_explicit_memory_worker(self) -> dict[str, int]:
        """Run existing authorized asynchronous processing; no test-side writes."""
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            result = run_extraction_tick(
                conn, worker_id="mvp-00a-explicit-memory",
            )
        return dict(vars(result))

    def observe_state(self) -> dict[str, int]:
        """Read-only evidence scoped to the fixture's user and persona.

        A SQL read-only transaction enforces observation-only behavior even
        if a later refactor accidentally adds a write to this method.
        """
        counts: dict[str, int] = {}
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                with conn.cursor(row_factory=dict_row) as cur:
                    for label, table in _SCOPED_TABLES.items():
                        # Table names come only from a fixed internal allowlist.
                        cur.execute(
                            f"SELECT count(*) AS n FROM {table} "
                            "WHERE user_id=%s AND instance_id=%s",
                            (self.identity.user_id, self.identity.instance_id),
                        )
                        counts[label] = cur.fetchone()["n"]
                    cur.execute(
                        """SELECT count(*) AS n FROM conversation_responses
                           WHERE user_id=%s AND instance_id=%s
                             AND delivery_status='DELIVERED'""",
                        (self.identity.user_id, self.identity.instance_id),
                    )
                    counts["delivered_responses"] = cur.fetchone()["n"]
        return counts
