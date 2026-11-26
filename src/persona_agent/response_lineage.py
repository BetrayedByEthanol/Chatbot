"""Conversation-owned durable read fences; source authority stays with owners.

A scope revision is deliberately conservative, not a per-record dependency
graph. Exact recall references and epistemic status remain in existing
Memory recall observations/events, joined by response_id.
"""
import json

from .conversation_contracts import (
    HistoricalReadFence,PreferenceReadFence,StaleConversation,StaleHistoricalContext,
    InvalidatedResponse,
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
    cur.execute(
        """SELECT preference_key,status,version FROM relationship_preferences
           WHERE user_id=%s AND instance_id=%s AND scope_key=''
             AND preference_key=ANY(%s::text[])
           ORDER BY preference_key FOR SHARE""",
        (user_id,instance_id,list(fence.keys)),
    )
    observed=tuple((row["preference_key"],row["status"],row["version"])
                   for row in cur.fetchall())
    if observed!=fence.revisions:
        raise StaleConversation("Relationship preference changed during recall")


def persist_response_lineage(cur,*,response_id,user_id,instance_id,historical,preference):
    keys=preference.keys if preference is not None else ()
    revisions=preference.revisions if preference is not None else ()
    if historical is None and not keys:
        return
    cur.execute(
        """INSERT INTO conversation_response_lineage(
               response_id,user_id,instance_id,historical_revision,next_expiry,
               preference_keys,preference_revisions
           ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (response_id,user_id,instance_id,
         historical.revision if historical is not None else None,
         historical.next_expiry if historical is not None else None,
         list(keys),json.dumps(revisions)),
    )


def require_response_lineage(cur,user_id,instance_id,response_id):
    """Call inside short scoped output transaction, after selection/conversation locks.

    No locks over model calls, synthesis, client I/O or playback. A denial
    never rewrites response text, receipt or historical delivery status.
    """
    cur.execute(
        """SELECT historical_revision,next_expiry,preference_keys,
                  preference_revisions,legacy_unbound
           FROM conversation_response_lineage
           WHERE response_id=%s AND user_id=%s AND instance_id=%s""",
        (response_id,user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        return
    if row["legacy_unbound"]:
        raise InvalidatedResponse("response has no recorded source fence")
    try:
        # Same lock order as response commit: Relationship then historical.
        if row["preference_keys"]:
            require_preference_fence(cur,user_id,instance_id,PreferenceReadFence(
                tuple(row["preference_keys"]),
                tuple(tuple(v) for v in row["preference_revisions"]),
            ))
        if row["historical_revision"] is not None:
            require_historical_read_fence(cur,user_id,instance_id,HistoricalReadFence(
                row["historical_revision"],row["next_expiry"],
            ))
    except (StaleHistoricalContext,StaleConversation) as exc:
        raise InvalidatedResponse("response evidence no longer current") from exc
