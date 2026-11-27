"""Bounded foreground raw-history projection and exact response dependencies.

Conversation owns chronology and provenance; Memory owns source eligibility.
No raw-content copies, semantic revocation, or owner writes occur here.
"""
from .conversation_contracts import ModelMessage, StaleHistoricalContext

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
    responses retain their established omission policy.
    """
    cur.execute(
        """SELECT t.turn_id,t.sequence,t.input_text,
                  response_lineage_derived_text(r.response_id,r.user_id,r.instance_id,
                      r.knowledge_excerpt,r.response_text) AS response_text,
                  r.delivery_status,
                  raw.source_turn_ids,raw.reusable,
                  (r.knowledge_excerpt OR entity_response_has_evidence(r.response_id,r.user_id,r.instance_id)
                   OR EXISTS (
                      SELECT 1 FROM conversation_response_lineage l
                      WHERE l.response_id=r.response_id AND l.user_id=r.user_id
                        AND l.instance_id=r.instance_id
                  )) AS broad_omitted
           FROM conversation_turns t
           LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
             AND r.user_id=t.user_id AND r.instance_id=t.instance_id
             AND r.conversation_id=t.conversation_id
           LEFT JOIN conversation_response_raw_lineage raw ON raw.response_id=r.response_id
             AND raw.user_id=r.user_id AND raw.instance_id=r.instance_id
             AND raw.conversation_id=r.conversation_id
           WHERE t.conversation_id=%s AND t.user_id=%s AND t.instance_id=%s
             AND t.sequence<=%s
           ORDER BY t.sequence DESC LIMIT %s""",
        (conversation_id,user_id,instance_id,sequence,max_turns),
    )
    recent=list(reversed(cur.fetchall()))
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
            else:
                add(seq,"assistant",OMITTED_ASSISTANT)
        for index,followup in enumerate(followups.get(anchor,())):
            deps=followup_sources.get((anchor,index))
            if deps is not None and deps<=eligible:
                add(seq,"assistant",followup["response_text"],deps)
            else:
                add(seq,"assistant",OMITTED_FOLLOWUP)
    return recent,history,sequences,dependencies,turn_id in eligible


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
