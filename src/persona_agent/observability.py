"""P1-03 read-only conversation observability, derived from durable records.

Time-to-output-ready is measurable for the current non-streaming transport.
True time-to-first-token, GPU budget and TTS playback are not yet measured;
none of these values are invented from generated or delivered timestamps.
"""

from datetime import datetime
from statistics import mean
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from .contracts import TrustedActor
from .conversation import _require_actor
from .operation_contracts import require_idle
from .runtime import PermissionDenied


class TurnTrace(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    conversation_id: UUID
    turn_id: UUID
    sequence: int
    turn_status: str
    input_at: datetime
    response_id: UUID | None
    model_id: str | None
    response_status: str | None
    output_ready_ms: float | None
    delivered_ms: float | None
    operation_id: UUID | None
    operation_status: str | None
    followup_status: str | None
    followup_delivered_ms: float | None
    event_ids: tuple[UUID,...]


class ConversationMetrics(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    conversation_id: UUID
    sample_size: int
    generated_count: int
    delivered_count: int
    pending_turn_count: int
    active_tool_count: int
    deferred_followup_count: int
    oldest_active_tool_age_seconds: float | None
    mean_output_ready_ms: float | None
    p95_output_ready_ms: float | None
    mean_delivery_ms: float | None
    p95_delivery_ms: float | None


def _millis(delta) -> float | None:
    return round(delta.total_seconds()*1000,2) if delta is not None else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values.sort()
    # Nearest-rank P95; documented rather than implying precision from
    # non-streaming response events.
    import math
    return values[math.ceil(0.95*len(values))-1]


def get_turn_trace(
    connection: Connection, actor: TrustedActor, turn_id: UUID,
) -> TurnTrace:
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.conversation_id,t.turn_id,t.sequence,
                       t.status AS turn_status,t.created_at AS input_at,
                       r.response_id,r.model_id,r.delivery_status AS response_status,
                       r.generated_at,r.delivered_at,
                       c.operation_id,o.status AS operation_status,
                       c.followup_state AS followup_status,
                       followup.delivered_at AS followup_at
                FROM conversation_turns t
                LEFT JOIN conversation_responses r
                  ON r.turn_id=t.turn_id AND r.user_id=t.user_id
                LEFT JOIN conversation_tool_calls c
                  ON c.turn_id=t.turn_id AND c.user_id=t.user_id
                LEFT JOIN operations o
                  ON o.operation_id=c.operation_id
                 AND o.user_id=t.user_id AND o.instance_id=t.instance_id
                LEFT JOIN conversation_assistant_followups followup
                  ON followup.tool_call_id=c.tool_call_id
                 AND followup.user_id=t.user_id
                WHERE t.turn_id=%s AND t.user_id=%s
                """,
                (turn_id,actor.user_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("turn trace outside authenticated scope")
            # The event journal contains stable identifiers, not raw text.
            cur.execute(
                """
                SELECT event_id FROM events
                WHERE user_id=%s AND instance_id=(
                    SELECT instance_id FROM conversations
                    WHERE conversation_id=%s AND user_id=%s
                )
                AND payload->>'turn_id'=%s
                ORDER BY recorded_at,event_id LIMIT 32
                """,
                (actor.user_id,row["conversation_id"],actor.user_id,str(turn_id)),
            )
            events=tuple(a["event_id"] for a in cur.fetchall())
            return TurnTrace(
                conversation_id=row["conversation_id"],turn_id=turn_id,
                sequence=row["sequence"],turn_status=row["turn_status"],
                input_at=row["input_at"],response_id=row["response_id"],
                model_id=row["model_id"],response_status=row["response_status"],
                output_ready_ms=(
                    _millis(row["generated_at"]-row["input_at"])
                    if row["generated_at"] is not None else None
                ),
                delivered_ms=(
                    _millis(row["delivered_at"]-row["input_at"])
                    if row["delivered_at"] is not None else None
                ),
                operation_id=row["operation_id"],
                operation_status=row["operation_status"],
                followup_status=row["followup_status"],
                followup_delivered_ms=(
                    _millis(row["followup_at"]-row["input_at"])
                    if row["followup_at"] is not None else None
                ),
                event_ids=events,
            )


def get_conversation_metrics(
    connection: Connection, actor: TrustedActor, conversation_id: UUID,
    *, max_turns: int=200,
) -> ConversationMetrics:
    require_idle(connection)
    _require_actor(actor)
    if not 1<=max_turns<=500:
        raise ValueError("metrics sample must cover 1..500 turns")
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM conversations WHERE conversation_id=%s AND user_id=%s",
                (conversation_id,actor.user_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("conversation metrics outside authenticated scope")
            cur.execute(
                """
                SELECT t.status AS turn_status,t.created_at,
                       r.generated_at,r.delivered_at
                FROM conversation_turns t
                LEFT JOIN conversation_responses r
                  ON r.turn_id=t.turn_id AND r.user_id=t.user_id
                WHERE t.conversation_id=%s AND t.user_id=%s
                ORDER BY t.sequence DESC LIMIT %s
                """,
                (conversation_id,actor.user_id,max_turns),
            )
            turns=cur.fetchall()
            cur.execute(
                """
                SELECT count(*) FILTER (
                         WHERE o.status IN ('QUEUED','RUNNING','RETRY_WAIT')
                       ) AS active_tool_count,
                       count(*) FILTER (
                         WHERE c.followup_state='DEFERRED'
                       ) AS deferred_followup_count,
                       min(c.created_at) FILTER (
                         WHERE o.status IN ('QUEUED','RUNNING','RETRY_WAIT')
                       ) AS oldest_active_at,
                       clock_timestamp() AS snapshot_time
                FROM conversation_tool_calls c
                LEFT JOIN operations o
                  ON o.operation_id=c.operation_id
                 AND o.user_id=c.user_id AND o.instance_id=c.instance_id
                WHERE c.conversation_id=%s AND c.user_id=%s
                """,
                (conversation_id,actor.user_id),
            )
            backlog=cur.fetchone()
    ready=[_millis(t["generated_at"]-t["created_at"])
           for t in turns if t["generated_at"] is not None]
    delivered=[_millis(t["delivered_at"]-t["created_at"])
               for t in turns if t["delivered_at"] is not None]
    return ConversationMetrics(
        conversation_id=conversation_id,sample_size=len(turns),
        generated_count=len(ready),delivered_count=len(delivered),
        pending_turn_count=sum(t["turn_status"]=="PENDING" for t in turns),
        active_tool_count=backlog["active_tool_count"],
        deferred_followup_count=backlog["deferred_followup_count"],
        oldest_active_tool_age_seconds=(
            max(0.0,round((backlog["snapshot_time"]-backlog["oldest_active_at"]).total_seconds(),2))
            if backlog["oldest_active_at"] is not None else None
        ),
        mean_output_ready_ms=round(mean(ready),2) if ready else None,
        p95_output_ready_ms=_p95(ready),
        mean_delivery_ms=round(mean(delivered),2) if delivered else None,
        p95_delivery_ms=_p95(delivered),
    )
