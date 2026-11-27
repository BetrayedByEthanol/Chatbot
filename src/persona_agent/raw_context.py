"""Bounded foreground raw-history projection and exact response dependencies.

Conversation owns chronology and provenance; Memory owns source eligibility.
No raw-content copies, semantic revocation, or owner writes occur here.
"""
from datetime import datetime,timezone

from .conversation_contracts import HistoricalReadFence,ModelMessage,StaleHistoricalContext

MAX_RAW_SOURCES=128
OMITTED_USER="[Earlier user statement omitted: source no longer eligible.]"
OMITTED_ASSISTANT="[Evidence-bearing assistant text omitted; request fresh recall.]"
OMITTED_FOLLOWUP="[Earlier tool follow-up omitted: source no longer eligible.]"


def eligible_turn_ids(cur,user_id,instance_id,conversation_id,ids):
    if not ids:
        return set()
    cur.execute(
        """SELECT raw_context_eligible_turns(%s,%s,%s,%s::uuid[]) AS turn_id""",
        (user_id,instance_id,conversation_id,list(ids)),
    )
    return {row["turn_id"] for row in cur.fetchall()}


def project_recent_context(cur,*,user_id,instance_id,conversation_id,turn_id,
                           sequence,max_turns):
    """Return chronological messages and their exact reusable raw dependencies.

    Prior response bodies inherit the bounded union of their source IDs.
    Prefer the latest response/follow-up when the lineage budget is tight;
    the user turns themselves always have priority. Existing broad-fenced
    content-dependent responses retain their established omission policy.
    A current presentation-only reply may be reused with its inherited
    scope/expiry fence, in addition to the exact raw dependency IDs.
    """
    cur.execute(
        """SELECT t.turn_id,t.sequence,t.input_text,
                  response_lineage_derived_text(r.response_id,r.user_id,r.instance_id,
                      r.knowledge_excerpt,r.response_text) AS response_text,
                  r.delivery_status,r.response_text AS stored_response_text,
                  raw.source_turn_ids,raw.reusable,raw.legacy_unbound AS raw_unbound,
                  l.presentation_only,l.historical_revision,l.next_expiry,
                  h.revision AS current_historical_revision,
                  (r.knowledge_excerpt OR entity_response_has_evidence(
                      r.response_id,r.user_id,r.instance_id)) AS content_guarded,
                  (r.knowledge_excerpt OR entity_response_has_evidence(
                      r.response_id,r.user_id,r.instance_id)
                   OR l.response_id IS NOT NULL) AS broad_omitted
           FROM conversation_turns t
           LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
             AND r.user_id=t.user_id AND r.instance_id=t.instance_id
             AND r.conversation_id=t.conversation_id
           LEFT JOIN conversation_response_raw_lineage raw ON raw.response_id=r.response_id
             AND raw.user_id=r.user_id AND raw.instance_id=r.instance_id
             AND raw.conversation_id=r.conversation_id
           LEFT JOIN conversation_response_lineage l ON l.response_id=r.response_id
             AND l.user_id=r.user_id AND l.instance_id=r.instance_id
           LEFT JOIN historical_recall_revisions h ON h.user_id=t.user_id
             AND h.instance_id=t.instance_id
           WHERE t.conversation_id=%s AND t.user_id=%s AND t.instance_id=%s
             AND t.sequence<=%s
           ORDER BY t.sequence DESC LIMIT %s""",
        (conversation_id,user_id,instance_id,sequence,max_turns),
    )
    recent=list(reversed(cur.fetchall()))
    now=datetime.now(timezone.utc)
    for row in recent:
        row["presentation_fence"]=None
        if (row["presentation_only"] and not row["content_guarded"]
                and row["reusable"] is True and not row["raw_unbound"]
                and row["source_turn_ids"] is not None
                and row["historical_revision"]==row["current_historical_revision"]
                and (row["next_expiry"] is None or row["next_expiry"]>now)):
            row["presentation_fence"]=HistoricalReadFence(
                row["historical_revision"],row["next_expiry"])
            row["broad_omitted"]=False
            row["response_text"]=row["stored_response_text"]
    anchors={row["turn_id"] for row in recent}
    cur.execute(
        """SELECT delivery_turn_id,turn_id AS source_turn_id,response_text
           FROM conversation_assistant_followups
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
             AND delivery_turn_id=ANY(%s::uuid[])
           ORDER BY delivered_at,tool_call_id""",
        (conversation_id,user_id,instance_id,list(anchors)),
    )
    followups={}
    for row in cur.fetchall():
        followups.setdefault(row["delivery_turn_id"],[]).append(row)

    # Select bounded possible lineage before the single batch eligibility
    # read. Do not silently drop a dependency to fit the budget.
    selected=set(anchors)
    def admit(ids):
        if len(selected | ids)>MAX_RAW_SOURCES:
            return False
        selected.update(ids)
        return True
    reply_sources={}
    followup_sources={}
    for row in reversed(recent):
        anchor=row["turn_id"]
        # Follow-ups appear after the primary response at the delivery turn.
        for index,followup in reversed(list(enumerate(followups.get(anchor,())))):
            deps={anchor,followup["source_turn_id"]}
            if admit(deps):
                followup_sources[(anchor,index)]=deps
        if row["delivery_status"]=="DELIVERED" and not row["broad_omitted"]:
            deps={anchor,*(row["source_turn_ids"] or ())}
            if row["reusable"] is not False and admit(deps):
                reply_sources[anchor]=deps
    eligible=eligible_turn_ids(cur,user_id,instance_id,conversation_id,selected)
    history=[]
    sequences=[]
    dependencies=[]
    presentation_fence=None
    def add(sequence,role,text,ids=()):
        history.append(ModelMessage(role,text))
        sequences.append(sequence)
        dependencies.append(frozenset(ids))

    for row in recent:
        anchor=row["turn_id"]
        seq=row["sequence"]
        current=anchor==turn_id
        row["source_eligible"]=anchor in eligible
        if not current and anchor not in eligible:
            row["input_text"]=OMITTED_USER
        add(seq,"user",row["input_text"],{anchor} if anchor in eligible else ())
        if row["delivery_status"]=="DELIVERED":
            deps=reply_sources.get(anchor)
            if anchor not in eligible:
                add(seq,"assistant",OMITTED_ASSISTANT)
            elif row["broad_omitted"]:
                add(seq,"assistant",row["response_text"])
            elif deps is not None and deps<=eligible:
                add(seq,"assistant",row["response_text"],deps)
                if row["presentation_fence"] is not None:
                    from .affect_lineage import merge_read_fences
                    presentation_fence=merge_read_fences(
                        presentation_fence,row["presentation_fence"])
            else:
                add(seq,"assistant",OMITTED_ASSISTANT)
        for index,followup in enumerate(followups.get(anchor,())):
            deps=followup_sources.get((anchor,index))
            if deps is not None and deps<=eligible:
                add(seq,"assistant",followup["response_text"],deps)
            else:
                add(seq,"assistant",OMITTED_FOLLOWUP)
    return recent,history,sequences,dependencies,turn_id in eligible,presentation_fence


def require_raw_context_fence(cur,user_id,instance_id,conversation_id,fence):
    """Serialize with deferred Memory-owner revisions, then check exact IDs.

    Call after domain/event writes or at the existing output boundary. No
    owner locks or external calls after the shared revision lock.
    """
    if not fence.source_turn_ids:
        return
    cur.execute(
        """SELECT revision FROM historical_recall_revisions
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise StaleHistoricalContext("raw history dependency scope unavailable")
    cur.execute(
        """SELECT raw_context_sources_current(%s,%s,%s,%s::uuid[]) AS current""",
        (user_id,instance_id,conversation_id,list(fence.source_turn_ids)),
    )
    if not cur.fetchone()["current"]:
        raise StaleHistoricalContext("raw history evidence changed during inference")


def persist_raw_context_lineage(cur,*,response_id,user_id,instance_id,conversation_id,fence):
    if fence is None:
        return
    cur.execute(
        """INSERT INTO conversation_response_raw_lineage(
               response_id,user_id,instance_id,conversation_id,source_turn_ids,reusable
           ) VALUES (%s,%s,%s,%s,%s,%s)""",
        (response_id,user_id,instance_id,conversation_id,list(fence.source_turn_ids),fence.reusable),
    )
