"""Conversation-owned durable read fences; source authority stays with owners.

Broad historical scope revisions remain conservative. Raw-context lineage
separately carries exact bounded turn IDs; owner policy is unchanged.
Recall references and epistemic status remain in Memory observations/events.
"""
import json

from .conversation_contracts import (
    HistoricalReadFence,PreferenceReadFence,StaleConversation,StaleHistoricalContext,
    InvalidatedResponse,RawContextFence,
)
from .memory_read_policy import require_historical_read_fence


def require_preference_fence(cur,user_id,instance_id,fence):
    # Relationship writes serialize on this state row, then preference rows.
    cur.execute(
        """SELECT 1 FROM relationship_state
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    cur.fetchone()
    # Acquire owner rows before the Memory eligibility watermark. Even
    # an ineligible stored revision remains the owner's concurrency record.
    cur.execute(
        """SELECT preference_key FROM relationship_preferences
           WHERE user_id=%s AND instance_id=%s AND scope_key=''
             AND preference_key=ANY(%s::text[])
           ORDER BY preference_key FOR SHARE""",
        (user_id,instance_id,list(fence.keys)),
    )
    cur.fetchall()
    from .workflow_evidence import lock_scope
    lock_scope(cur,user_id,instance_id)
    cur.execute(
        """SELECT preference_key,status,version FROM current_relationship_preferences
           WHERE user_id=%s AND instance_id=%s AND scope_key=''
             AND preference_key=ANY(%s::text[])
           ORDER BY preference_key""",
        (user_id,instance_id,list(fence.keys)),
    )
    observed=tuple((row["preference_key"],row["status"],row["version"])
                   for row in cur.fetchall())
    if observed!=fence.revisions:
        raise StaleConversation("Relationship preference changed during recall")


def persist_response_lineage(cur,*,response_id,user_id,instance_id,historical,preference,
                             presentation_only=False,predictive=None,subjective=None):
    keys=preference.keys if preference is not None else ()
    revisions=preference.revisions if preference is not None else ()
    if predictive is not None and (historical is None or presentation_only):
        raise ValueError("predictive lineage requires a historical fence and non-presentation reuse")
    if historical is None and not keys:
        return
    cur.execute(
        """INSERT INTO conversation_response_lineage(
               response_id,user_id,instance_id,historical_revision,next_expiry,
               preference_keys,preference_revisions,presentation_only,
               predictive_control_version,predictive_reset_generation,predictive_source_ids,
               subjective_expectation_id,subjective_outcome_turn_id,subjective_occasion,
               subjective_intention_id,subjective_kind,subjective_expires_at
           ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING response_id""",
        (response_id,user_id,instance_id,
         historical.revision if historical is not None else None,
         historical.next_expiry if historical is not None else None,
         list(keys),json.dumps(revisions),presentation_only,
         predictive.control_version if predictive is not None else None,
         predictive.reset_generation if predictive is not None else None,
         list(predictive.source_turn_ids) if predictive is not None else None,
         subjective.expectation_revision_id if subjective is not None else None,
         subjective.outcome_turn_id if subjective is not None else None,
         subjective.occasion if subjective is not None else None,
         subjective.intention_id if subjective is not None else None,
         subjective.kind if subjective is not None else None,
         subjective.expires_at if subjective is not None else None),
    )
    if cur.fetchone() is None:
        raise StaleHistoricalContext("subjective projection already reserved or response lineage exists")


def require_response_lineage(cur,user_id,instance_id,response_id):
    """Call inside short scoped output transaction, after selection/conversation locks.

    No locks over model calls, synthesis, client I/O or playback. A denial
    never rewrites response text, receipt or historical delivery status.
    """
    cur.execute(
        """SELECT historical_revision,next_expiry,preference_keys,
                  preference_revisions,legacy_unbound,predictive_control_version,
                  predictive_reset_generation,predictive_source_ids,
                  subjective_expectation_id,subjective_outcome_turn_id,subjective_occasion,
                  subjective_intention_id,subjective_kind,subjective_expires_at
           FROM conversation_response_lineage
           WHERE response_id=%s AND user_id=%s AND instance_id=%s""",
        (response_id,user_id,instance_id),
    )
    row=cur.fetchone()
    if row is not None and row["legacy_unbound"]:
        raise InvalidatedResponse("response has no recorded source fence")
    cur.execute(
        """SELECT conversation_id,source_turn_ids,reusable,legacy_unbound
           FROM conversation_response_raw_lineage
           WHERE response_id=%s AND user_id=%s AND instance_id=%s""",
        (response_id,user_id,instance_id),
    )
    raw=cur.fetchone()
    if raw is not None and raw["legacy_unbound"]:
        raise InvalidatedResponse("response has no recorded raw source fence")
    try:
        # Same lock order as response commit: Relationship then historical.
        if row is not None and row["preference_keys"]:
            require_preference_fence(cur,user_id,instance_id,PreferenceReadFence(
                tuple(row["preference_keys"]),
                tuple(tuple(v) for v in row["preference_revisions"]),
            ))
        if row is not None and row["historical_revision"] is not None:
            require_historical_read_fence(cur,user_id,instance_id,HistoricalReadFence(
                row["historical_revision"],row["next_expiry"],
            ))
        if raw is not None:
            from .raw_context import require_raw_context_fence
            require_raw_context_fence(cur,user_id,instance_id,raw["conversation_id"],
                RawContextFence(tuple(raw["source_turn_ids"]),raw["reusable"]))
        if row is not None and row["predictive_control_version"] is not None:
            from .learning_control_contracts import PredictiveFence
            from .predictive_admission import require_predictive_fence
            fence=PredictiveFence(row["predictive_control_version"],row["predictive_reset_generation"],
                tuple(row["predictive_source_ids"]),row["historical_revision"],row["next_expiry"])
            if row["subjective_expectation_id"] is not None:
                from .subjective_contracts import SubjectiveProjection
                from .subjective_context import require_subjective_projection
                require_subjective_projection(cur,user_id,instance_id,SubjectiveProjection(
                    row["subjective_expectation_id"],row["subjective_outcome_turn_id"],row["subjective_occasion"],
                    row["subjective_intention_id"],row["subjective_kind"],
                    row["subjective_expires_at"],fence,""))
            else:
                require_predictive_fence(cur,user_id,instance_id,fence)
        cur.execute("SELECT entity_response_has_evidence(%s,%s,%s) AS tracked",
                    (response_id,user_id,instance_id))
        if cur.fetchone()["tracked"]:
            # Entity owner writes bump the revision on commit. Lock only
            # at output; exact event pins also cover canonical report replies.
            from .workflow_evidence import lock_scope
            lock_scope(cur,user_id,instance_id)
            cur.execute("SELECT response_entity_lineage_current(%s,%s,%s) AS current",
                        (response_id,user_id,instance_id))
            if not cur.fetchone()["current"]:
                raise InvalidatedResponse("entity response evidence no longer current")
    except (StaleHistoricalContext,StaleConversation) as exc:
        raise InvalidatedResponse("response evidence no longer current") from exc
