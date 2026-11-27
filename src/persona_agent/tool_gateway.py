"""P1-02 tool gateway: immediate clock and durable read-only worker continuation.

All inference/provider execution is outside database transactions. The P0
operation scheduler remains the sole worker-claim/lease/recovery authority.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Callable, Literal
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import (
    _event, _require_actor, _selection, acknowledge_text_delivery,
    commit_text_response, lookup_response, prepare_direct_tool_context,
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
from .response_decisions import explicit_analysis_payload
from .workflow_evidence import lock_scope,source_current,require_source_current
from .conversation_contracts import RawContextFence
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
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("clock instant must be timezone-aware")
    context = prepare_direct_tool_context(connection, actor, turn_id)
    # Sample the trusted clock after the minimal scoped context read.
    instant = now or datetime.now(timezone.utc)
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
    prefix=f"Text analysis: {len(terms)} words, {len(set(terms))} unique. Top: "
    if not top:
        return prefix+"none."

    # The source input can itself be 12,000 characters long. Preserve the
    # statistics and term counts, reserving room for separators, the ellipsis
    # and the final period before rendering any term. Never send the worker
    # an oversized deterministic result it would retry without improvement.
    remaining=12000-len(prefix)-1  # final period
    rendered=[]
    for word,count in top:
        separator=", " if rendered else ""
        suffix=f" ({count})"
        allowed=remaining-len(separator)-len(suffix)
        if allowed < 1:
            break
        shown=word if len(word)<=allowed else word[:allowed-1]+"…"
        part=separator+shown+suffix
        rendered.append(part)
        remaining-=len(part)
        if len(shown)<len(word):
            break  # no further full term fits in the exhausted budget
    return prefix+"".join(rendered)+"."


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
    if actor.application_session_id is not None:
        raise PermissionDenied("application session has no durable tool admission grant")
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
                if not source_current(cur,actor.user_id,turn["instance_id"],
                                      turn["conversation_id"],command.turn_id):
                    raise ToolAuthorizationError("tool request evidence no longer eligible")
                cur.execute(
                    """
                    INSERT INTO conversation_tool_calls(
                        tool_call_id,conversation_id,turn_id,user_id,instance_id,
                        selection_generation,tool_name,tool_config
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING tool_call_id
                    """,
                    (tool_id,turn["conversation_id"],command.turn_id,actor.user_id,
                     turn["instance_id"],turn["selection_generation"],SLOW_TOOL,
                     '{"max_words":'+str(command.max_words)+'}'),
                )
                if cur.fetchone() is None:
                    # Two callers may race on BOTH the turn_id UNIQUE key
                    # and the deterministic tool_call_id PRIMARY KEY. Handle
                    # either conflict, then reread by the authenticated turn.
                    # This new statement sees the committed winner under
                    # READ COMMITTED even if our earlier SELECT saw nothing.
                    cur.execute(
                        "SELECT * FROM conversation_tool_calls "
                        "WHERE turn_id=%s FOR UPDATE",
                        (command.turn_id,),
                    )
                    existing=cur.fetchone()
                    if existing is None:
                        # A deterministic tool ID already belongs to a
                        # different turn. Never expose a raw PK violation or
                        # accidentally replay the wrong turn's operation.
                        raise IdempotencyConflict(
                            "tool identifier belongs to another turn"
                        )
                else:
                    _event(
                        cur,user_id=actor.user_id,instance_id=turn["instance_id"],
                        conversation_id=turn["conversation_id"],
                        event_type="conversation.tool.requested.v1",
                        source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
                        detail={"tool_call_id":str(tool_id),"turn_id":str(command.turn_id),
                                "tool_name":SLOW_TOOL},
                    )
            if existing is not None:
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
            SELECT t.input_text,c.tool_config,c.selection_generation,
                   c.conversation_id,c.turn_id,
                   dialogue_turn_current(c.user_id,c.instance_id,c.conversation_id,c.turn_id) AS eligible
            FROM conversation_tool_calls c
            JOIN conversation_turns t ON t.turn_id=c.turn_id
              AND t.user_id=c.user_id AND t.instance_id=c.instance_id
              AND t.conversation_id=c.conversation_id
            WHERE c.tool_call_id=%s AND c.user_id=%s AND c.instance_id=%s
            """,
            (lease.operation_id,lease.user_id,lease.instance_id),
        )
        payload=cur.fetchone()
    if payload is None or payload["selection_generation"]!=lease.selection_generation:
        fail_operation(connection,lease,reason="tool correlation missing or obsolete")
        return lease
    if not payload["eligible"]:
        fail_operation(connection,lease,reason="tool request evidence no longer eligible")
        return lease
    try:
        # External dependency injection MUST happen without database locks.
        # Auto-routed natural requests have an explicit, delimited payload.
        # Legacy operator-selected analysis of unprefixed text is unchanged.
        # The source is always the persisted authenticated user turn.
        source=payload["input_text"]
        analysis_text=explicit_analysis_payload(source) or source
        text=executor(analysis_text,payload["tool_config"]["max_words"])
        if not isinstance(text,str) or not 1<=len(text)<=12000 or not text.strip():
            raise ValueError("tool returned invalid text")
    except Exception as exc:
        fail_operation(connection,lease,reason=f"tool execution failed: {type(exc).__name__}")
        return lease
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            lock_scope(cur,lease.user_id,lease.instance_id)
            eligible=source_current(cur,lease.user_id,lease.instance_id,
                                    payload["conversation_id"],payload["turn_id"])
            if eligible:
                cur.execute(
                    """INSERT INTO conversation_tool_results(
                           operation_id,claim_generation,result_text
                       ) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (lease.operation_id,lease.claim_generation,text),
                )
    if not eligible:
        fail_operation(connection,lease,reason="tool request invalidated during execution")
        return lease
    # The P0 completion fence rejects expired/revoked claims. Orphaned
    # attempt results cannot be used unless COMPLETED references this attempt.
    complete_operation(
        connection,lease,
        result_ref=_result_ref(lease.operation_id,lease.claim_generation),
    )
    return lease


@dataclass(frozen=True)
class _ToolOutcome:
    """A deliverable outcome, not worker execution or permission."""
    kind: Literal["SUCCESS", "FAILED", "CANCELLED", "UNAVAILABLE"]
    text: str


def _read_outcome(cur, row) -> _ToolOutcome | None:
    """Read terminal evidence only; retry and cancellation requests are pending.

    A successful result must reference the exact completed worker attempt.
    Terminal failures have safe fixed notices, never provider error strings
    or an orphaned attempt's unconfirmed output. No operation state changes.
    """
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
    if op is None:
        return None
    if op["status"]=="FAILED":
        return _ToolOutcome("FAILED","The text analysis failed; I don't have a result.")
    if op["status"]=="CANCELLED":
        return _ToolOutcome("CANCELLED","The text analysis was cancelled.")
    if op["status"]!="COMPLETED":
        return None
    text=op["result_text"]
    if (op["result_ref"]!=_result_ref(row["operation_id"],op["claim_generation"])
            or not isinstance(text,str) or not text.strip()):
        # Inconsistent completion evidence must not masquerade as success
        # or leave the user waiting for a job the scheduler already ended.
        return _ToolOutcome(
            "UNAVAILABLE","I don't have a verified result for the text analysis.",
        )
    if not source_current(cur,row["user_id"],row["instance_id"],
                          row["conversation_id"],row["turn_id"]):
        return _ToolOutcome("UNAVAILABLE","The source of the text analysis is no longer available.")
    return _ToolOutcome("SUCCESS",text)


def _replay_delivered_followup(cur, row) -> ToolFollowup:
    """Replay acknowledged output only while its exact request is eligible."""
    lock_scope(cur,row["user_id"],row["instance_id"])
    if not source_current(cur,row["user_id"],row["instance_id"],
                          row["conversation_id"],row["turn_id"]):
        raise ToolResultNotReady("follow-up source no longer eligible")
    cur.execute(
        """SELECT response_text FROM conversation_assistant_followups
           WHERE tool_call_id=%s AND conversation_id=%s AND turn_id=%s
             AND user_id=%s AND instance_id=%s""",
        (row["tool_call_id"],row["conversation_id"],row["turn_id"],
         row["user_id"],row["instance_id"]),
    )
    saved=cur.fetchone()
    if saved is None:
        raise ToolResultNotReady("acknowledged follow-up text missing")
    return ToolFollowup(
        tool_call_id=row["tool_call_id"],operation_id=row["operation_id"],
        text=saved["response_text"],replayed=True,
    )


def _relevant_to_latest(cur, call, latest_turn_id: UUID) -> bool:
    """A later user turn is relevant only after an explicit thread link.

    A typed focus_thread command links the currently resumed input. Plain
    conversation turn arrival does not implicitly authorize old results;
    the resumed turn must also have a DELIVERED response before interrupting.
    """
    if latest_turn_id==call["turn_id"]:
        return True
    cur.execute(
        """
        SELECT 1 FROM dialogue_turn_links source
        JOIN dialogue_turn_links resumed
          ON resumed.conversation_id=source.conversation_id
         AND resumed.thread_id=source.thread_id
         AND resumed.user_id=source.user_id
         AND resumed.instance_id=source.instance_id
        JOIN conversations c
          ON c.conversation_id=source.conversation_id
         AND c.user_id=source.user_id
         AND c.instance_id=source.instance_id
         AND c.focus_thread_id=source.thread_id
        JOIN dialogue_threads thread
          ON thread.thread_id=source.thread_id
         AND thread.resolution_status='OPEN'
        JOIN conversation_responses last_reply
          ON last_reply.turn_id=resumed.turn_id
         AND last_reply.user_id=source.user_id
         AND last_reply.delivery_status='DELIVERED'
        WHERE source.turn_id=%s AND resumed.turn_id=%s
          AND source.conversation_id=%s AND source.user_id=%s
          AND c.last_input_turn_id=resumed.turn_id
        LIMIT 1
        """,
        (call["turn_id"],latest_turn_id,call["conversation_id"],call["user_id"]),
    )
    return cur.fetchone() is not None


def _validate_wait_budget(seconds: float) -> None:
    if not 0<=seconds<=5:
        raise ValueError("foreground wait budget must be within 0..5 seconds")


def _await_result(connection,actor,tool_id,seconds:float) -> _ToolOutcome | None:
    _validate_wait_budget(seconds)
    end=time.monotonic()+seconds
    while True:
        with connection.cursor(row_factory=dict_row) as cur:
            row=_read_call(cur,actor.user_id,tool_id)
            result=_read_outcome(cur,row)
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
    # Validate before staging a call or enqueueing durable work, even on retry.
    _validate_wait_budget(await_seconds)
    existing=lookup_response(connection,actor,command.turn_id)
    if existing is not None:
        # Preserve P1-01's authority checks for previously generated text:
        # no unsafe shortcut returning historical/generated output.
        from .conversation import respond_to_text
        class NeverRun:
            def generate(self,messages):
                raise AssertionError("historical response should be replayed")
        return respond_to_text(connection,actor,command.turn_id,NeverRun())
    context=prepare_direct_tool_context(connection,actor,command.turn_id)
    from dataclasses import replace
    context=replace(context,raw_context_fence=RawContextFence((command.turn_id,),True))
    tool=schedule_text_analysis(connection,actor,command)
    result=_await_result(connection,actor,tool.tool_call_id,await_seconds)
    if result is not None:
        reply=ModelReply(model_id=_DIRECT_MODEL,text=result.text)
    else:
        reply=ModelReply(
            model_id=_DEFERRED_MODEL,
            text="I've submitted the text analysis and will follow up when its outcome is available.",
        )
    return commit_text_response(connection,actor,context,reply)


def reconcile_tool_followup(
    connection: Connection, actor: TrustedActor, tool_call_id: UUID,
) -> ToolCallResult:
    """Durably compute a conservative follow-up policy for terminal outcomes.

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
            lock_scope(cur,row["user_id"],row["instance_id"])
            eligible=source_current(cur,row["user_id"],row["instance_id"],
                                    row["conversation_id"],row["turn_id"])
            outcome=_read_outcome(cur,row) if eligible else None
            if (
                not eligible or selection is None or selection["instance_id"]!=row["instance_id"]
                or selection["generation"]!=row["selection_generation"]
                or selection["mode"]!="FOREGROUND"
            ):
                status="SUPPRESSED"
            elif mode=="DIRECT":
                status="SUPPRESSED"  # outcome was already used in initial response
            elif mode is None or outcome is None:
                status="PENDING"
            elif (
                response["delivery_status"]!="DELIVERED"
                or not _relevant_to_latest(
                    cur,row,conversation["last_input_turn_id"]
                )
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
                    detail={
                        "tool_call_id":str(tool_call_id),"status":status,
                        "outcome":outcome.kind if outcome is not None else None,
                    },
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
            if current is None or not _relevant_to_latest(
                cur,row,current["last_input_turn_id"]
            ):
                raise StaleConversation("tool follow-up no longer relevant")
            # Re-read after selection and conversation locks: another
            # acknowledgment/reconciliation may have changed the old row.
            row=_read_call(cur,actor.user_id,tool_call_id,lock=True)
            if row["followup_state"]!="READY":
                raise ToolResultNotReady("tool follow-up not ready")
            lock_scope(cur,row["user_id"],row["instance_id"])
            if not source_current(cur,row["user_id"],row["instance_id"],
                                  row["conversation_id"],row["turn_id"]):
                raise ToolResultNotReady("follow-up source no longer eligible")
            outcome=_read_outcome(cur,row)
            if outcome is None:
                raise ToolResultNotReady("terminal outcome unavailable")
            return ToolFollowup(
                tool_call_id=tool_call_id,operation_id=row["operation_id"],text=outcome.text,
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
                return _replay_delivered_followup(cur,row)
            _selection(cur,actor,row["instance_id"],row["selection_generation"])
            cur.execute(
                "SELECT last_input_turn_id FROM conversations "
                "WHERE conversation_id=%s AND user_id=%s FOR UPDATE",
                (row["conversation_id"],actor.user_id),
            )
            conv=cur.fetchone()
            row=_read_call(cur,actor.user_id,tool_call_id,lock=True)
            if row["followup_state"]=="DELIVERED":
                return _replay_delivered_followup(cur,row)
            if conv is None or not _relevant_to_latest(
                cur,row,conv["last_input_turn_id"]
            ):
                raise StaleConversation("follow-up superseded before acknowledgment")
            if row["followup_state"]!="READY":
                raise ToolResultNotReady("tool follow-up not ready")
            lock_scope(cur,row["user_id"],row["instance_id"])
            if not source_current(cur,row["user_id"],row["instance_id"],
                                  row["conversation_id"],row["turn_id"]):
                raise ToolResultNotReady("follow-up source no longer eligible")
            outcome=_read_outcome(cur,row)
            if outcome is None:
                raise ToolResultNotReady("terminal outcome unavailable")
            text=outcome.text
            # Preserve the original request turn as provenance but anchor
            # this assistant output at the user turn active when delivery
            # was acknowledged. The conversation lock serializes the anchor
            # against concurrent ingest and prevents chronology rewrites.
            cur.execute(
                """
                INSERT INTO conversation_assistant_followups(
                    tool_call_id,conversation_id,turn_id,delivery_turn_id,
                    user_id,instance_id,response_text
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (tool_call_id,row["conversation_id"],row["turn_id"],
                 conv["last_input_turn_id"],actor.user_id,
                 row["instance_id"],text),
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
                detail={
                    "tool_call_id":str(tool_call_id),"operation_id":str(row["operation_id"]),
                    "outcome":outcome.kind,
                },
            )
            return ToolFollowup(
                tool_call_id=tool_call_id,operation_id=row["operation_id"],text=text,
            )
