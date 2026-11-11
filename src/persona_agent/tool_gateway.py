"""P1-02 tool gateway: immediate clock and durable read-only worker continuation.

All inference/provider execution is outside database transactions. The P0
operation scheduler remains the sole worker-claim/lease/recovery authority.
"""

from collections import Counter
from datetime import datetime, timezone
import re
import time
from typing import Callable
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import (
    _event, _require_actor, _selection, acknowledge_text_delivery,
    commit_text_response, lookup_response, prepare_text_context,
    respond_to_text,
)
from .conversation_contracts import ModelReply, StaleConversation, TextResponseResult
from .operation_contracts import (
    EffectClass, EnqueueOperation, OperationLease, Policy, Priority,
    StaleOperation, require_idle,
)
from .operation_scheduler import claim_operation, enqueue_operation
from .operation_control import complete_operation, fail_operation
from .runtime import IdempotencyConflict, PermissionDenied, StaleSelection
from .tool_contracts import (
    RESOURCE_KEY, SLOW_TOOL, TIME_TOOL, SlowTextToolCommand,
    ToolCallResult, ToolFollowup, ToolAuthorizationError, ToolResultNotReady,
)

_DIRECT_MODEL="tool.initial.direct.v1"
_DEFERRED_MODEL="tool.initial.deferred.v1"
_TOOL_ID_BASE="persona-agent/turn-tool/"


def respond_with_time(
    connection: Connection, actor: TrustedActor, turn_id: UUID,
    *, timezone_name: str = "UTC", now: datetime | None = None,
) -> TextResponseResult:
    """Typed, deterministic immediate read-only tool; no external provider."""
    require_idle(connection)
    _require_actor(actor)
    # Preserve typed argument validation on both new and replayed calls.
    # A retry must not silently accept a malformed timezone identifier.
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown timezone") from exc
    prior=lookup_response(connection,actor,turn_id)
    if prior is not None:
        # Reuse P1-01's selected-persona / latest-turn validation for
        # GENERATED responses; lookup_response alone is historical access.
        # An already delivered clock answer is replayed as persisted.
        class NoRegeneration:
            def generate(self,messages):
                raise AssertionError("persisted clock response must not regenerate")
        return respond_to_text(connection,actor,turn_id,NoRegeneration())
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("clock instant must be timezone-aware")
    context = prepare_text_context(connection, actor, turn_id)
    value = instant.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
    return commit_text_response(
        connection, actor, context,
        ModelReply(model_id=TIME_TOOL, text=f"Current time in {timezone_name}: {value}"),
    )


def analyze_text(text: str, max_words: int) -> str:
    """Sample bounded slow-capability implementation: deterministic text statistics.

    This implementation is quick; it deliberately runs through the slow
    scheduler to prove async behavior without involving arbitrary URLs,
    provider credentials, sandbox execution, or external side effects.
    """
    terms=re.findall(r"\b\w+\b",text.casefold())
    top=Counter(terms).most_common(max_words)
    formatted=", ".join(f"{word} ({count})" for word,count in top) or "none"
    return f"Text analysis: {len(terms)} words, {len(set(terms))} unique. Top: {formatted}."


def _tool_id(turn_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL,_TOOL_ID_BASE+str(turn_id)+"/"+SLOW_TOOL)


def _result_ref(operation_id: UUID, generation: int) -> str:
    return f"tool-result:{operation_id}:{generation}"


def _read_call(cursor, user_id: UUID, tool_call_id: UUID, *, lock=False):
    cursor.execute(
        """
        SELECT * FROM conversation_tool_calls
        WHERE tool_call_id=%s AND user_id=%s
        """ + (" FOR UPDATE" if lock else ""),
        (tool_call_id,user_id),
    )
    row=cursor.fetchone()
    if row is None:
        raise PermissionDenied("tool call outside authenticated scope")
    return row


def _typed(row) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=row["tool_call_id"],operation_id=row["operation_id"],
        admission_state=row["admission_state"],
        initial_reply_mode=row["initial_reply_mode"],
        followup_state=row["followup_state"],
    )


def schedule_text_analysis(
    connection: Connection, actor: TrustedActor, command: SlowTextToolCommand,
) -> ToolCallResult:
    """Persist correlation FIRST; invoke authenticated P0 enqueue separately.

    The PREPARING row survives a crash between staging and enqueue; retry
    creates the same UUID/command receipt, then finalizes the link.
    A deployment must configure pool 'tool.readonly' before admitting work.
    """
    require_idle(connection)
    _require_actor(actor)
    tool_id=_tool_id(command.turn_id)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.conversation_id,t.instance_id,t.selection_generation,
                       t.status,c.last_input_turn_id
                FROM conversation_turns t
                JOIN conversations c ON c.conversation_id=t.conversation_id
                WHERE t.turn_id=%s AND t.user_id=%s
                """,(command.turn_id,actor.user_id),
            )
            turn=cur.fetchone()
            if turn is None:
                raise PermissionDenied("turn outside authenticated scope")
            cur.execute(
                "SELECT * FROM conversation_tool_calls WHERE turn_id=%s FOR UPDATE",
                (command.turn_id,),
            )
            existing=cur.fetchone()
            if existing is None:
                _selection(cur,actor,turn["instance_id"],turn["selection_generation"])
                if turn["status"]!="PENDING" or turn["last_input_turn_id"]!=command.turn_id:
                    raise StaleConversation("only the latest pending turn may schedule tools")
                cur.execute(
                    """
                    INSERT INTO conversation_tool_calls(
                        tool_call_id,conversation_id,turn_id,user_id,instance_id,
                        selection_generation,tool_name,tool_config
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (tool_id,turn["conversation_id"],command.turn_id,actor.user_id,
                     turn["instance_id"],turn["selection_generation"],SLOW_TOOL,
                     '{"max_words":'+str(command.max_words)+'}'),
                )
                _event(
                    cur,user_id=actor.user_id,instance_id=turn["instance_id"],
                    conversation_id=turn["conversation_id"],
                    event_type="conversation.tool.requested.v1",
                    source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
                    detail={"tool_call_id":str(tool_id),"turn_id":str(command.turn_id),
                            "tool_name":SLOW_TOOL},
                )
            else:
                if (
                    existing["tool_call_id"]!=tool_id or
                    existing["tool_name"]!=SLOW_TOOL or
                    existing["tool_config"]!={"max_words":command.max_words}
                ):
                    raise IdempotencyConflict("turn already has a different tool call")
                if existing["admission_state"]=="QUEUED":
                    return _typed(existing)

    # No locks are held during this cross-domain authenticated admission.
    op=EnqueueOperation(
        operation_id=tool_id,instance_id=turn["instance_id"],
        expected_generation=turn["selection_generation"],
        idempotency_key=f"tool-{tool_id}",operation_type=SLOW_TOOL,
        execution_policy=Policy.FOREGROUND_ASYNC,
        priority_class=Priority.P1,effect_class=EffectClass.READ_ONLY,
        resources={RESOURCE_KEY:1},capabilities=(SLOW_TOOL,),
    )
    enqueue_operation(connection,actor,op)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            row=_read_call(cur,actor.user_id,tool_id,lock=True)
            if row["operation_id"] is not None and row["operation_id"]!=tool_id:
                raise StaleConversation("tool admission correlation mismatch")
            if row["admission_state"]=="PREPARING":
                cur.execute(
                    """
                    UPDATE conversation_tool_calls SET
                        operation_id=%s,admission_state='QUEUED',
                        updated_at=clock_timestamp()
                    WHERE tool_call_id=%s AND user_id=%s
                    RETURNING *
                    """,
                    (tool_id,tool_id,actor.user_id),
                )
                row=cur.fetchone()
            return _typed(row)


def read_tool_call(
    connection: Connection, actor: TrustedActor, tool_call_id: UUID,
) -> ToolCallResult:
    """Scoped historical status read, not authorization to send results."""
    require_idle(connection)
    _require_actor(actor)
    with connection.cursor(row_factory=dict_row) as cur:
        return _typed(_read_call(cur,actor.user_id,tool_call_id))


def run_one_text_tool(
    connection: Connection, *,
    worker_id: str,
    executor: Callable[[str,int],str] = analyze_text,
    lease_seconds: int = 120,
) -> OperationLease | None:
    """Claim exactly a registered slow tool; do not steal other operations."""
    require_idle(connection)
    lease=claim_operation(
        connection,worker_id=worker_id,capabilities=(SLOW_TOOL,),
        operation_types=(SLOW_TOOL,),lease_seconds=lease_seconds,
    )
    if lease is None:
        return None
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT t.input_text,c.tool_config,c.selection_generation
            FROM conversation_tool_calls c
            JOIN conversation_turns t ON t.turn_id=c.turn_id
            WHERE c.tool_call_id=%s AND c.user_id=%s AND c.instance_id=%s
            """,
            (lease.operation_id,lease.user_id,lease.instance_id),
        )
        payload=cur.fetchone()
    if payload is None or payload["selection_generation"]!=lease.selection_generation:
        fail_operation(connection,lease,reason="tool correlation missing or obsolete")
        return lease
    try:
        # External dependency injection MUST happen without database locks.
        text=executor(payload["input_text"],payload["tool_config"]["max_words"])
        if not isinstance(text,str) or not 1<=len(text)<=12000:
            raise ValueError("tool returned invalid text")
    except Exception as exc:
        fail_operation(connection,lease,reason=f"tool execution failed: {type(exc).__name__}")
        return lease
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO conversation_tool_results(
                operation_id,claim_generation,result_text
            ) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """,
            (lease.operation_id,lease.claim_generation,text),
        )
    # The P0 completion fence rejects expired/revoked claims. Orphaned
    # attempt results cannot be used unless COMPLETED references this attempt.
    complete_operation(
        connection,lease,
        result_ref=_result_ref(lease.operation_id,lease.claim_generation),
    )
    return lease


def _completed_result(cur, row) -> str | None:
    if row["operation_id"] is None:
        return None
    cur.execute(
        """
        SELECT o.status,o.result_ref,o.claim_generation,r.result_text
        FROM operations o
        LEFT JOIN conversation_tool_results r
          ON r.operation_id=o.operation_id
         AND r.claim_generation=o.claim_generation
        WHERE o.operation_id=%s AND o.user_id=%s AND o.instance_id=%s
          AND o.operation_type=%s
        """,
        (row["operation_id"],row["user_id"],row["instance_id"],SLOW_TOOL),
    )
    op=cur.fetchone()
    if (op is None or op["status"]!="COMPLETED"
        or op["result_ref"]!=_result_ref(row["operation_id"],op["claim_generation"])):
        return None
    return op["result_text"]


def _await_result(connection,actor,tool_id,seconds:float) -> str | None:
    if not 0<=seconds<=5:
        raise ValueError("foreground wait budget must be within 0..5 seconds")
    end=time.monotonic()+seconds
    while True:
        with connection.cursor(row_factory=dict_row) as cur:
            row=_read_call(cur,actor.user_id,tool_id)
            result=_completed_result(cur,row)
        if result is not None:
            return result
        remaining=end-time.monotonic()
        if remaining<=0:
            return None
        time.sleep(min(0.025,remaining))


def respond_to_text_analysis(
    connection: Connection, actor: TrustedActor, command: SlowTextToolCommand,
    *, await_seconds: float=0.05,
) -> TextResponseResult:
    """Bounded synchronous wait; persist a concise defer notice if still running.

    The committed model_id is a durable direct/defer decision so a restart
    can reconcile tool follow-up even if it crashes before another callback.
    """
    require_idle(connection)
    _require_actor(actor)
    existing=lookup_response(connection,actor,command.turn_id)
    if existing is not None:
        # Preserve P1-01's authority checks for previously generated text:
        # no unsafe shortcut returning historical/generated output.
        from .conversation import respond_to_text
        class NeverRun:
            def generate(self,messages):
                raise AssertionError("historical response should be replayed")
        return respond_to_text(connection,actor,command.turn_id,NeverRun())
    context=prepare_text_context(connection,actor,command.turn_id)
    tool=schedule_text_analysis(connection,actor,command)
    result=_await_result(connection,actor,tool.tool_call_id,await_seconds)
    if result is not None:
        reply=ModelReply(model_id=_DIRECT_MODEL,text=result)
    else:
        reply=ModelReply(
            model_id=_DEFERRED_MODEL,
            text="I'm processing the text analysis and will follow up when it is ready.",
        )
    return commit_text_response(connection,actor,context,reply)


def reconcile_tool_followup(
    connection: Connection, actor: TrustedActor, tool_call_id: UUID,
) -> ToolCallResult:
    """Durably compute a conservative follow-up policy after worker completion.

    No automatic delivery: the adapter must explicitly fetch and acknowledge.
    New conversation input -> DEFERRED; persona revocation -> SUPPRESSED.
    """
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            # Scope check before selecting the authority row; access to this
            # tool link does not grant authority for another user's instance.
            row=_read_call(cur,actor.user_id,tool_call_id)
            if row["followup_state"] in ("DELIVERED","SUPPRESSED"):
                return _typed(row)
            cur.execute(
                "SELECT instance_id,generation,mode FROM persona_selection "
                "WHERE user_id=%s FOR SHARE",
                (actor.user_id,),
            )
            selection=cur.fetchone()
            cur.execute(
                "SELECT last_input_turn_id FROM conversations "
                "WHERE conversation_id=%s AND user_id=%s FOR UPDATE",
                (row["conversation_id"],actor.user_id),
            )
            conversation=cur.fetchone()
            row=_read_call(cur,actor.user_id,tool_call_id,lock=True)
            if row["followup_state"] in ("DELIVERED","SUPPRESSED"):
                return _typed(row)
            cur.execute(
                "SELECT delivery_status,model_id FROM conversation_responses "
                "WHERE turn_id=%s AND user_id=%s",
                (row["turn_id"],actor.user_id),
            )
            response=cur.fetchone()
            mode=(
                "DIRECT" if response and response["model_id"]==_DIRECT_MODEL else
                "DEFERRED" if response and response["model_id"]==_DEFERRED_MODEL else None
            )
            done=_completed_result(cur,row)
            if (
                selection is None or selection["instance_id"]!=row["instance_id"]
                or selection["generation"]!=row["selection_generation"]
                or selection["mode"]!="FOREGROUND"
            ):
                status="SUPPRESSED"
            elif mode=="DIRECT":
                status="SUPPRESSED"  # result was already used in initial response
            elif mode is None or done is None:
                status="PENDING"
            elif (
                conversation["last_input_turn_id"]!=row["turn_id"]
                or response["delivery_status"]!="DELIVERED"
            ):
                status="DEFERRED"
            else:
                status="READY"
            if (status!=row["followup_state"] or
                row["initial_reply_mode"]!=mode):
                cur.execute(
                    """
                    UPDATE conversation_tool_calls
                    SET followup_state=%s,initial_reply_mode=%s,
                        updated_at=clock_timestamp()
                    WHERE tool_call_id=%s AND user_id=%s RETURNING *
                    """,
                    (status,mode,tool_call_id,actor.user_id),
                )
                row=cur.fetchone()
                _event(
                    cur,user_id=actor.user_id,instance_id=row["instance_id"],
                    conversation_id=row["conversation_id"],
                    event_type="conversation.tool.followup_state.v1",
                    source_id="tool.gateway",trust_level="TRUSTED_PLATFORM",
                    detail={"tool_call_id":str(tool_call_id),"status":status},
                )
            return _typed(row)


def prepare_tool_followup(
    connection: Connection, actor: TrustedActor, tool_call_id: UUID,
) -> ToolFollowup:
    """Only READY, relevant, selected-persona results may be shown."""
    state=reconcile_tool_followup(connection,actor,tool_call_id)
    if state.followup_state!="READY":
        raise ToolResultNotReady(f"follow-up not authorized: {state.followup_state}")
    require_idle(connection)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            row=_read_call(cur,actor.user_id,tool_call_id)
            _selection(cur,actor,row["instance_id"],row["selection_generation"])
            cur.execute(
                "SELECT last_input_turn_id FROM conversations "
                "WHERE conversation_id=%s AND user_id=%s FOR SHARE",
                (row["conversation_id"],actor.user_id),
            )
            current=cur.fetchone()
            if current is None or current["last_input_turn_id"]!=row["turn_id"]:
                raise StaleConversation("tool follow-up no longer relevant")
            if row["followup_state"]!="READY":
                raise ToolResultNotReady("tool follow-up not ready")
            text=_completed_result(cur,row)
            if text is None:
                raise ToolResultNotReady("completed result unavailable")
            return ToolFollowup(
                tool_call_id=tool_call_id,operation_id=row["operation_id"],text=text,
            )


def acknowledge_tool_followup(
    connection: Connection, actor: TrustedActor, tool_call_id: UUID,
) -> ToolFollowup:
    """Commit a delivered follow-up after the output channel sends its text."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            row=_read_call(cur,actor.user_id,tool_call_id)
            if row["followup_state"]=="DELIVERED":
                text=_completed_result(cur,row)
                if text is None:
                    raise ToolResultNotReady("prior delivered tool result missing")
                return ToolFollowup(
                    tool_call_id=tool_call_id,operation_id=row["operation_id"],
                    text=text,replayed=True,
                )
            _selection(cur,actor,row["instance_id"],row["selection_generation"])
            cur.execute(
                "SELECT last_input_turn_id FROM conversations "
                "WHERE conversation_id=%s AND user_id=%s FOR UPDATE",
                (row["conversation_id"],actor.user_id),
            )
            conv=cur.fetchone()
            row=_read_call(cur,actor.user_id,tool_call_id,lock=True)
            if row["followup_state"]=="DELIVERED":
                text=_completed_result(cur,row)
                return ToolFollowup(
                    tool_call_id=tool_call_id,operation_id=row["operation_id"],
                    text=text,replayed=True,
                )
            if conv is None or conv["last_input_turn_id"]!=row["turn_id"]:
                raise StaleConversation("follow-up superseded before acknowledgment")
            if row["followup_state"]!="READY":
                raise ToolResultNotReady("tool follow-up not ready")
            text=_completed_result(cur,row)
            if text is None:
                raise ToolResultNotReady("completed result unavailable")
            # This is an assistant output, not a fabricated user turn.
            # Persist the exact acknowledged text with its source turn so
            # later context assembly can reconstruct what the user received.
            cur.execute(
                """
                INSERT INTO conversation_assistant_followups(
                    tool_call_id,conversation_id,turn_id,user_id,instance_id,
                    response_text
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (tool_call_id,row["conversation_id"],row["turn_id"],
                 actor.user_id,row["instance_id"],text),
            )
            cur.execute(
                "UPDATE conversation_tool_calls SET followup_state='DELIVERED',"
                "updated_at=clock_timestamp() WHERE tool_call_id=%s AND user_id=%s",
                (tool_call_id,actor.user_id),
            )
            # Delivered assistant content changes the usable dialogue
            # context and must advance optimistic conversation revision.
            cur.execute(
                "UPDATE conversations SET revision=revision+1,"
                "updated_at=clock_timestamp() WHERE conversation_id=%s "
                "AND user_id=%s AND instance_id=%s",
                (row["conversation_id"],actor.user_id,row["instance_id"]),
            )
            _event(
                cur,user_id=actor.user_id,instance_id=row["instance_id"],
                conversation_id=row["conversation_id"],
                event_type="conversation.tool.followup_delivered.v1",
                source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
                detail={"tool_call_id":str(tool_call_id),"operation_id":str(row["operation_id"])},
            )
            return ToolFollowup(
                tool_call_id=tool_call_id,operation_id=row["operation_id"],text=text,
            )
