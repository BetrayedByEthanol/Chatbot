"""MVP-01D conversation-owned, bounded natural-language response dispatch.

Selection from the persisted authenticated input occurs in the application
orchestration layer, not in the local transport adapter. Registered tools
remain authoritative over admission, side effects, persistence and recovery.
"""

from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import respond_to_text, _require_actor
from .conversation_contracts import TextModelGateway, TextResponseResult
from .operation_contracts import require_idle
from .response_decisions import decide_response
from .runtime import PermissionDenied
from .tool_contracts import SlowTextToolCommand
from .tool_gateway import respond_to_text_analysis, respond_with_time


def respond_to_user_text(
    connection: Connection, actor: TrustedActor, turn_id: UUID,
    gateway: TextModelGateway,
    *, default_timezone: str = "UTC", max_words: int = 5,
    await_seconds: float = 0.05,
) -> TextResponseResult:
    """Make one high-precision response decision for an authenticated turn.

    No operation is admitted based on model-generated content. This read is
    not held open during inference or worker admission; the chosen existing
    domain response path rechecks selection/current-turn/revision fencing.
    """
    require_idle(connection)
    _require_actor(actor)
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT t.input_text
               FROM conversation_turns t
               JOIN conversations c ON c.conversation_id=t.conversation_id
                 AND c.user_id=t.user_id AND c.instance_id=t.instance_id
               WHERE t.turn_id=%s AND t.user_id=%s""",
            (turn_id,actor.user_id),
        )
        item=cur.fetchone()
    if item is None:
        raise PermissionDenied("response input outside authenticated scope")

    decision=decide_response(item["input_text"],default_timezone=default_timezone)
    if decision.route=="CLOCK":
        return respond_with_time(
            connection,actor,turn_id,timezone_name=decision.timezone_name,
        )
    if decision.route=="TEXT_ANALYSIS":
        return respond_to_text_analysis(
            connection,actor,SlowTextToolCommand(
                turn_id=turn_id,max_words=max_words,
            ),await_seconds=await_seconds,
        )
    return respond_to_text(connection,actor,turn_id,gateway)
