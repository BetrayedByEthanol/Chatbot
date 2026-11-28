"""Selective subjective context: reuse Cognitive and Emotion ownership.

Only an exact new report with a current prior tentative expectation can
contribute. No raw learned prose, strong surprise, mood update, unsolicited
message, tool authority or new inference. All text is host-authored.
"""
import json
from datetime import timedelta
from uuid import NAMESPACE_URL,uuid4,uuid5

from psycopg.rows import dict_row

from .affect_context import tentative_reaction_hint
from .conversation_contracts import StaleHistoricalContext
from .predictive_admission import capture_predictive_fence,require_predictive_fence
from .prediction_lifecycle import _expectation,_latest,_now
from .contextual_expectations import _authorize
from .operation_contracts import require_idle
from .runtime import PermissionDenied,VersionConflict,IdempotencyConflict,_fingerprint
from .subjective_contracts import (
    SubjectiveProjection,CancelSubjectiveIntention,IntentionCancellationResult,
)


def stage_support_intention(cur,work,comparison_id,comparison):
    """Cognitive owner: one structured intent from an admitted interpretation.

    Bind the *new* expectation revision produced by the same range (not the
    comparison's historical prior). No model-authored trigger or content is
    reinterpreted as permission. Existing generic intentions stay proposed.
    """
    if comparison.completed or comparison.read.classification not in ("EXPECTED","UNEXPECTED"):
        return
    r=comparison.read
    old=_expectation(cur,work.user_id,work.instance_id,r.expectation_revision_id)
    if old is None:
        return
    latest=_latest(cur,work.user_id,work.instance_id,r.fence.reset_generation,old["context_key"])
    if (latest is None or latest["operation_id"]!=work.operation_id
            or latest["disposition"]!="HYPOTHESIS" or latest["maturity"]!="TENTATIVE"
            or latest["direction"] not in ("COMPLETION","NON_COMPLETION")
            or latest["control_version"]!=r.fence.control_version):
        return
    now=_now(cur)
    expiry=min(latest["expires_at"],old["expires_at"],now+timedelta(days=1))
    if expiry<=now:
        return
    # The source comparison and latest revision were staged in the same
    # optional savepoint; their existing late fences cover this intent.
    cur.execute("""INSERT INTO cognitive_intentions(
                    intention_id,user_id,instance_id,comparison_id,expectation_revision_id,
                    condition_kind,condition_context,trigger_text,expires_at,delivery_constraints)
                   VALUES (%s,%s,%s,%s,%s,'OFFER_SUPPORT_ON_NON_COMPLETION',%s,
                           'Later independent non-completion report for same activity',%s,
                           '{"requires_attention_approval":true,"requires_response_scheduler":true,
                             "foreground_current_report_only":true}'::jsonb)
                   ON CONFLICT(comparison_id) DO NOTHING""",
                (uuid4(),work.user_id,work.instance_id,comparison_id,latest["revision_id"],
                 old["context_key"],expiry))


def _projection_current(cur,user_id,instance_id,p):
    f=p.fence
    cur.execute("""SELECT subjective_projection_current(%s,%s,%s,%s,%s::uuid[],
                    %s,%s,%s,%s,%s) AS current""",
                (user_id,instance_id,f.control_version,f.reset_generation,list(f.source_turn_ids),
                 p.expectation_revision_id,p.outcome_turn_id,p.intention_id,p.kind,p.expires_at))
    return cur.fetchone()["current"]


def require_subjective_projection(cur,user_id,instance_id,p):
    require_predictive_fence(cur,user_id,instance_id,p.fence)
    if not _projection_current(cur,user_id,instance_id,p):
        raise StaleHistoricalContext("subjective expectation, intention or boundary changed")


def prepare_subjective_projection(cur,user_id,instance_id,comparison,*,max_chars):
    """Add at most one host-selected intention and one bounded affect hint.

    Called after mandatory routes and existing context budgeting. Never evict
    current input or existing context. No SQL for non-comparison routes.
    """
    if max_chars<240 or comparison.fence is None or comparison.classification not in ("EXPECTED","UNEXPECTED"):
        return None
    f=comparison.fence
    e=_expectation(cur,user_id,instance_id,comparison.expectation_revision_id)
    if e is None or e["maturity"]!="TENTATIVE":
        return None
    cur.execute("""SELECT reported_completion,reported_context,reported_at
                   FROM predictive_input_admissions WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
                (comparison.outcome_turn_id,user_id,instance_id))
    report=cur.fetchone()
    if report is None:
        return None
    intent=None
    if report["reported_completion"] is False:
        # Candidate-first, exact context; the SQL validation below checks
        # the complete condition and no contrary source. No generic trigger
        # matching, no callback or scheduling on a different topic.
        cur.execute("""SELECT i.intention_id,i.expires_at,c.source_turn_ids
                      FROM cognitive_intentions i
                      JOIN cognitive_expectation_comparisons c
                        ON c.comparison_id=i.comparison_id AND c.user_id=i.user_id AND c.instance_id=i.instance_id
                      WHERE i.user_id=%s AND i.instance_id=%s AND i.condition_context=%s
                        AND i.condition_kind='OFFER_SUPPORT_ON_NON_COMPLETION'
                        AND i.expectation_revision_id=%s AND i.status='PROPOSED' AND NOT i.already_asked
                        AND i.created_at<%s AND i.expires_at>clock_timestamp()
                        AND NOT EXISTS (SELECT 1 FROM conversation_response_lineage l
                            WHERE l.subjective_intention_id=i.intention_id)
                      ORDER BY i.created_at DESC,i.intention_id LIMIT 1""",
                    (user_id,instance_id,report["reported_context"],e["revision_id"],report["reported_at"]))
        intent=cur.fetchone()
    mismatch=comparison.classification=="UNEXPECTED"
    if not mismatch and intent is None:
        return None
    # No strong surprise at tentative maturity. Emotion's pure presentation
    # helper returns a fixed, non-judgmental hint; persistent mood untouched.
    text=tentative_reaction_hint(completed=report["reported_completion"],mismatch=mismatch,
                                offer_support=intent is not None)
    if not text or len(text)>max_chars:
        return None
    ids=tuple(dict.fromkeys((*f.source_turn_ids,*(intent["source_turn_ids"] if intent else ()))))
    if len(ids)>128:
        return None
    captured=capture_predictive_fence(cur,user_id,instance_id,ids)
    if (captured is None or captured.control_version!=f.control_version
            or captured.reset_generation!=f.reset_generation):
        return None
    # Preserve the oldest observed revision and earliest expiry from the
    # actual comparison; a later capture cannot refresh a stale read.
    expiry=min(comparison.expires_at,e["expires_at"],report["reported_at"]+timedelta(hours=1),
               *([intent["expires_at"]] if intent else []))
    from .learning_control_contracts import PredictiveFence
    time_bounds=[x for x in (f.next_expiry,captured.next_expiry,expiry) if x is not None]
    combined=PredictiveFence(f.control_version,f.reset_generation,ids,
                             min(f.historical_revision,captured.historical_revision),min(time_bounds))
    projection=SubjectiveProjection(e["revision_id"],comparison.outcome_turn_id,
                                    intent["intention_id"] if intent else None,
                                    "MISMATCH" if mismatch else "SUPPORT",expiry,combined,text)
    try:
        require_subjective_projection(cur,user_id,instance_id,projection)
    except StaleHistoricalContext:
        return None
    return projection


def inspect_subjective_intentions(connection,actor,instance_id,*,limit=20):
    """Bounded metadata-only review. Reserved is not fulfilled or delivered.

    Exact context and source content remain private and are never returned
    from this history endpoint. Current-turn selection validates all pins.
    """
    require_idle(connection)
    if not 1<=limit<=100:
        raise ValueError("invalid inspection limit")
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        cur.execute("""WITH candidates AS MATERIALIZED (
                         SELECT * FROM cognitive_intentions
                         WHERE user_id=%s AND instance_id=%s AND condition_kind IS NOT NULL
                         ORDER BY created_at DESC,intention_id LIMIT %s)
                       SELECT i.intention_id,i.expectation_revision_id,i.comparison_id,
                              i.created_at,i.expires_at,i.status,i.already_asked,
                              CASE WHEN i.status='CANCELLED' THEN 'CANCELLED'
                                   WHEN l.response_id IS NOT NULL THEN 'RESERVED'
                                   WHEN i.expires_at<=clock_timestamp() THEN 'EXPIRED'
                                   WHEN NOT COALESCE(c.learning_enabled,false) THEN 'DISABLED'
                                   WHEN c.reset_generation<>e.reset_generation THEN 'RESET'
                                   WHEN c.version<>e.control_version THEN 'CONTROL_CHANGED'
                                   WHEN e.disposition<>'HYPOTHESIS' OR EXISTS (
                                       SELECT 1 FROM cognitive_expectation_revisions newer
                                       WHERE newer.user_id=i.user_id AND newer.instance_id=i.instance_id
                                         AND newer.reset_generation=e.reset_generation
                                         AND newer.context_key=e.context_key AND newer.version>e.version)
                                       THEN 'SUPERSEDED'
                                   ELSE 'REQUIRES_CONDITION_AND_SOURCE_CHECK' END AS effective_status
                       FROM candidates i JOIN cognitive_expectation_revisions e
                         ON e.revision_id=i.expectation_revision_id AND e.user_id=i.user_id
                         AND e.instance_id=i.instance_id
                       LEFT JOIN predictive_learning_controls c
                         ON c.user_id=i.user_id AND c.instance_id=i.instance_id
                       LEFT JOIN conversation_response_lineage l
                         ON l.subjective_intention_id=i.intention_id
                       ORDER BY i.created_at DESC,i.intention_id""",
                    (actor.user_id,instance_id,limit))
        return tuple(dict(r) for r in cur.fetchall())


def cancel_subjective_intention(connection,actor,command:CancelSubjectiveIntention):
    """Authenticated exact cancellation; no new response or mood mutation."""
    require_idle(connection)
    if type(command) is not CancelSubjectiveIntention:
        raise TypeError("expected CancelSubjectiveIntention")
    from .conversation import _selection
    kind="cognition.subjective_intention.cancel.v1"
    fingerprint=_fingerprint(command)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,command.instance_id)
        _selection(cur,actor,command.instance_id,command.expected_generation)
        receipt=uuid4()
        cur.execute("""INSERT INTO command_receipts(receipt_id,user_id,instance_id,command_type,
                       idempotency_key,request_fingerprint) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING RETURNING receipt_id""",
                    (receipt,actor.user_id,command.instance_id,kind,command.idempotency_key,fingerprint))
        if cur.fetchone() is None:
            cur.execute("""SELECT result_json,request_fingerprint FROM command_receipts
                           WHERE user_id=%s AND instance_id=%s AND command_type=%s AND idempotency_key=%s""",
                        (actor.user_id,command.instance_id,kind,command.idempotency_key))
            saved=cur.fetchone()
            if saved is None or saved["request_fingerprint"]!=fingerprint or saved["result_json"] is None:
                raise IdempotencyConflict("intention cancellation key reused")
            return IntentionCancellationResult.model_validate(saved["result_json"]).model_copy(
                update={"replayed":True})
        cur.execute("""SELECT status FROM cognitive_intentions
                       WHERE intention_id=%s AND user_id=%s AND instance_id=%s
                         AND condition_kind='OFFER_SUPPORT_ON_NON_COMPLETION'""",
                    (command.intention_id,actor.user_id,command.instance_id))
        row=cur.fetchone()
        if row is None:
            raise PermissionDenied("subjective intention outside authenticated scope")
        if row["status"]!="PROPOSED":
            raise VersionConflict("subjective intention no longer proposed")
        # No key update is compatible with response-lineage FK validation.
        # Stage all owner/event writes before the shared late validation gate.
        cur.execute("""UPDATE cognitive_intentions SET status='CANCELLED'
                       WHERE intention_id=%s AND user_id=%s AND instance_id=%s AND status='PROPOSED'
                       RETURNING intention_id""",(command.intention_id,actor.user_id,command.instance_id))
        if cur.fetchone() is None:
            raise VersionConflict("subjective intention changed")
        stream=uuid5(NAMESPACE_URL,f"persona-agent/{command.instance_id}/cognition.subjective_intention")
        cur.execute("""INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
                       VALUES (%s,%s,%s,'cognition.subjective_intention') ON CONFLICT DO NOTHING""",
                    (stream,actor.user_id,command.instance_id))
        cur.execute("""UPDATE event_streams SET last_sequence=last_sequence+1
                       WHERE stream_id=%s AND user_id=%s AND instance_id=%s RETURNING last_sequence""",
                    (stream,actor.user_id,command.instance_id))
        seq=cur.fetchone()["last_sequence"]
        event_id=uuid4()
        cur.execute("""INSERT INTO events(event_id,event_type,schema_version,user_id,instance_id,
                       stream_id,stream_sequence,source_id,trust_level,correlation_id,payload)
                       VALUES (%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb)""",
                    (event_id,kind,actor.user_id,command.instance_id,stream,seq,actor.source_id,receipt,
                     json.dumps({"intention_id":str(command.intention_id)})))
        cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",(uuid4(),event_id))
        cur.execute("""SELECT version FROM predictive_learning_controls
                       WHERE user_id=%s AND instance_id=%s FOR NO KEY UPDATE""",
                    (actor.user_id,command.instance_id))
        if cur.fetchone() is None:
            raise VersionConflict("learning control unavailable")
        result=IntentionCancellationResult(intention_id=command.intention_id,event_id=event_id)
        cur.execute("UPDATE command_receipts SET result_json=%s::jsonb,event_id=%s WHERE receipt_id=%s",
                    (result.model_dump_json(),event_id,receipt))
        return result
