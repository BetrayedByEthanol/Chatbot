"""Selective subjective context: reuse Cognitive and Emotion ownership.

Only an exact new report with a current prior tentative expectation can
contribute. No raw learned prose, strong surprise, mood update, unsolicited
message, tool authority or new inference. All text is host-authored.
"""
from datetime import timedelta
from uuid import uuid4

from .affect_context import tentative_reaction_hint
from .conversation_contracts import StaleHistoricalContext
from .predictive_admission import capture_predictive_fence,require_predictive_fence
from .prediction_lifecycle import _expectation,_latest,_control,_now
from .subjective_contracts import SubjectiveProjection


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
    expiry=min(comparison.expires_at,e["expires_at"],*([intent["expires_at"]] if intent else []))
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
