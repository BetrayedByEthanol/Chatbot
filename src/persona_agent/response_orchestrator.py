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
from .preference_recall import respond_with_preference_recall
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
            """SELECT t.input_text,t.temporal_context,t.conversation_id,t.instance_id,t.sequence
               FROM conversation_turns t
               JOIN conversations c ON c.conversation_id=t.conversation_id
                 AND c.user_id=t.user_id AND c.instance_id=t.instance_id
               WHERE t.turn_id=%s AND t.user_id=%s""",
            (turn_id,actor.user_id),
        )
        item=cur.fetchone()
    if item is None:
        raise PermissionDenied("response input outside authenticated scope")

    # Use the persisted ingress zone, not a different host setting on retry.
    # An absent zone retains the existing explicitly labeled UTC fallback;
    # the temporal snapshot itself never treats that fallback as user-local.
    snapshot=item["temporal_context"]
    zone=(snapshot["timezone_name"] or "UTC"
          if snapshot is not None else default_timezone)
    decision=decide_response(item["input_text"],default_timezone=zone)
    if decision.route=="PREFERENCE_RECALL":
        return respond_with_preference_recall(connection,actor,turn_id)
    if decision.route=="MEANING_RECALL":
        from .personal_meaning import respond_with_meaning_recall
        return respond_with_meaning_recall(connection,actor,turn_id)
    if decision.route=="PERSONAL_FACT_RECALL":
        from .personal_fact_recall import respond_with_personal_fact_recall
        return respond_with_personal_fact_recall(connection,actor,turn_id)
    if decision.route in ("ENTITY_RECALL","ENTITY_REPORT"):
        from .entity_recall import respond_with_entity_recall
        return respond_with_entity_recall(
            connection,actor,turn_id,report=decision.route=="ENTITY_REPORT",
        )
    # A short relation phrase is not a standalone command. Only a previous
    # delivered grounded choice makes it a candidate for a direct reply.
    from .entity_reference import entity_clarification_relation
    if entity_clarification_relation(item["input_text"]) is not None:
        from .entity_recall import adjacent_entity_clarification,respond_with_entity_recall
        with connection.cursor(row_factory=dict_row) as cur:
            pending=adjacent_entity_clarification(
                cur,user_id=actor.user_id,instance_id=item["instance_id"],
                conversation_id=item["conversation_id"],sequence=item["sequence"],
            )
        if pending is not None:
            return respond_with_entity_recall(connection,actor,turn_id)
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
