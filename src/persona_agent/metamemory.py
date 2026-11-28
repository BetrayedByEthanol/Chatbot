"""Memory-owned scoped recall observations and user-confirmed relevance feedback.

Latest observations describe the last read, not continuously verified truth.
No model-generated assessment is treated as feedback. No record content is copied.
"""
import json
from uuid import UUID,uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor,_selection
from .memory import _receipt,_finish,_event
from .metamemory_contracts import (
    RecallObservation,RetrievalFeedbackCommand,RetrievalFeedbackResult,
    recall_key,recall_outcome,candidate_key,feedback_phrase,
)
from .operation_contracts import require_idle
from .runtime import PermissionDenied,VersionConflict


def safe_query_key(route,topic):
    from .historical_user_turns import _SENSITIVE
    if not topic or len(topic)>180 or _SENSITIVE.search(topic):
        return None
    return recall_key(route,topic)


def entity_topic(reference):
    if reference is None or reference.adjacent or not reference.name_key:
        return None
    return f"{reference.kind}/{reference.relation or 'ANY'}/{reference.name_key}"


def prior_clarification(cur,*,user_id,instance_id,query_key,candidates,before_at):
    """Suppress only an unchanged previously delivered named choice for 24h."""
    if query_key is None or len(candidates)!=2:
        return False
    cur.execute("""SELECT memory_recall_choice_awaiting(%s,%s,%s,%s,%s) AS awaiting""",
                (user_id,instance_id,query_key,candidate_key(candidates),before_at))
    return cur.fetchone()["awaiting"]


def adjacent_clarification_key(cur,*,user_id,instance_id,conversation_id,sequence,before_at):
    """Resolution can update only the immediately preceding named ambiguity."""
    cur.execute(
        """SELECT o.query_key FROM memory_recall_observations o
           JOIN conversation_turns t ON t.turn_id=o.turn_id AND t.conversation_id=o.conversation_id
             AND t.user_id=o.user_id AND t.instance_id=o.instance_id
           JOIN conversation_responses r ON r.response_id=o.response_id
             AND r.user_id=o.user_id AND r.instance_id=o.instance_id
           WHERE o.user_id=%s AND o.instance_id=%s AND o.conversation_id=%s
             AND t.sequence=%s AND o.route='ENTITY' AND o.outcome='AMBIGUOUS'
             AND o.clarification_key IS NOT NULL AND r.delivery_status='DELIVERED'
             AND memory_recall_response_current(o.response_id,o.user_id,o.instance_id,
                                                o.conversation_id,o.turn_id)
             AND r.delivered_at<=%s""",
        (user_id,instance_id,conversation_id,sequence-1,before_at),
    )
    row=cur.fetchone()
    return row["query_key"] if row else None


def record_recall(cur,*,user_id,instance_id,conversation_id,turn_id,response_id,
                  route,query_key,status,evidence_refs=(),candidates=(),suppressed=False):
    """Called by Conversation only inside the successful response commit.

    Existing grounded response events/outbox are the audit source; this
    projection contains just identifiers, outcome and a normalized key.
    """
    if query_key is None:
        return
    refs=tuple(evidence_refs)[:16]
    clarification=candidate_key(candidates) if status=="AMBIGUOUS" and len(candidates)==2 else None
    cur.execute(
        """INSERT INTO memory_recall_observations(
               response_id,user_id,instance_id,conversation_id,turn_id,route,query_key,
               outcome,evidence_refs,clarification_key,clarification_suppressed
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
        (response_id,user_id,instance_id,conversation_id,turn_id,route,query_key,
         recall_outcome(status),json.dumps(refs,separators=(",",":")),clarification,suppressed),
    )


def _scope(cur,actor,instance_id):
    cur.execute("SELECT 1 FROM persona_instances WHERE id=%s AND user_id=%s",
                (instance_id,actor.user_id))
    if cur.fetchone() is None:
        raise PermissionDenied("MetaMemory instance outside authenticated scope")


def list_recall_observations(connection: Connection,actor: TrustedActor,instance_id: UUID,
                             *,route=None,topic=None,open_only=False,attention_only=False,limit=30):
    """History, or current eligibility of the latest observed topic gap.

    History retains original outcomes/feedback. An invalidated AVAILABLE
    observation reopens a gap without waiting for another user query.
    A withdrawn request is unavailable, not an active gap. Do not recover
    an older observation for that key. Attention is a bounded read of open
    or review-required gaps, excluding already-awaiting clarification.
    It does not create an intention, schedule work or ask a question.
    """
    require_idle(connection)
    _require_actor(actor)
    if (not 1<=limit<=100 or (attention_only and not open_only) or (topic is not None and (route is None or not isinstance(topic,str)
            or not 1<=len(topic)<=180)) or (route is not None and route not in (
                "FACT","MEANING","ENTITY","ENTITY_HISTORY","PATTERN","KNOWLEDGE"))):
        raise ValueError("invalid MetaMemory query")
    key=safe_query_key(route,topic) if topic is not None else None
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,actor,instance_id)
            if topic is not None and key is None:
                return ()
            cur.execute(
                """WITH ranked AS (
                       SELECT o.*,
                              count(*) FILTER (WHERE o.outcome<>'BUDGET') OVER(
                                  PARTITION BY o.route,o.query_key
                                  ORDER BY o.observed_at DESC,o.response_id DESC
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                              ) AS nonbudget_rank
                       FROM memory_recall_observations o
                       WHERE o.user_id=%s AND o.instance_id=%s
                         AND (%s::text IS NULL OR o.route=%s)
                         AND (%s::text IS NULL OR o.query_key=%s)
                         AND (NOT %s OR o.outcome<>'BUDGET')
                   ), candidates AS MATERIALIZED (
                       SELECT * FROM ranked
                       WHERE NOT %s OR nonbudget_rank=1
                       ORDER BY observed_at DESC,response_id DESC
                       LIMIT CASE WHEN %s THEN NULL ELSE %s END
                   ), observed AS MATERIALIZED (
                       SELECT o.response_id,o.conversation_id,o.turn_id,o.route,o.query_key,o.outcome,
                              o.evidence_refs,o.clarification_key,o.clarification_suppressed,
                              o.observed_at,r.delivered_at,o.nonbudget_rank,
                              workflow_source_current(o.user_id,o.instance_id,o.conversation_id,
                                                      o.turn_id,NULL) AS request_current,
                              memory_recall_response_current(o.response_id,o.user_id,o.instance_id,
                                  o.conversation_id,o.turn_id) AS source_current,
                              feedback.history AS all_feedback,feedback.current AS current_feedback
                       FROM candidates o
                       JOIN conversation_responses r ON r.response_id=o.response_id
                         AND r.user_id=o.user_id AND r.instance_id=o.instance_id
                       CROSS JOIN LATERAL (
                           SELECT COALESCE(jsonb_agg(f.outcome ORDER BY f.recorded_at,f.feedback_id),
                                           '[]'::jsonb) AS history,
                                  COALESCE(jsonb_agg(f.outcome ORDER BY f.recorded_at,f.feedback_id)
                                      FILTER (WHERE workflow_source_current(f.user_id,f.instance_id,
                                                          f.conversation_id,f.feedback_turn_id,NULL)),
                                      '[]'::jsonb) AS current
                           FROM memory_retrieval_feedback f
                           WHERE f.response_id=o.response_id AND f.user_id=o.user_id
                             AND f.instance_id=o.instance_id
                       ) feedback
                   ), effective AS (
                       SELECT *,
                          CASE
                            WHEN NOT request_current THEN 'UNAVAILABLE'
                            WHEN outcome='BUDGET' THEN 'BUDGET_OMITTED'
                            WHEN nonbudget_rank>1 THEN 'SUPERSEDED'
                            WHEN current_feedback ? 'INCORRECT' OR current_feedback ? 'MISSED'
                                THEN 'REVIEW_REQUIRED'
                            WHEN NOT source_current THEN 'OPEN'
                            WHEN outcome='AVAILABLE' THEN 'RESOLVED'
                            WHEN outcome='AMBIGUOUS' AND delivered_at IS NOT NULL
                              AND clarification_key IS NOT NULL
                              AND memory_recall_choice_awaiting(%s,%s,query_key,clarification_key,
                                                                statement_timestamp())
                              THEN 'AWAITING_CLARIFICATION'
                            ELSE 'OPEN'
                          END AS gap_state
                       FROM observed
                   )
                   SELECT response_id,conversation_id,turn_id,route,query_key,
                          CASE WHEN %s AND outcome='AVAILABLE' AND NOT source_current
                               THEN 'STALE' ELSE outcome END AS outcome,
                          CASE WHEN %s AND NOT source_current THEN '[]'::jsonb
                               ELSE evidence_refs END AS evidence_refs,
                          CASE WHEN %s AND NOT source_current THEN false
                               ELSE clarification_suppressed END AS clarification_suppressed,
                          observed_at,delivered_at,
                          CASE WHEN %s THEN current_feedback ELSE all_feedback END AS feedback,
                          source_current,request_current,gap_state
                   FROM effective WHERE (NOT %s OR gap_state IN (
                       'OPEN','AWAITING_CLARIFICATION','REVIEW_REQUIRED'
                   )) AND (NOT %s OR gap_state IN ('OPEN','REVIEW_REQUIRED'))
                   ORDER BY CASE WHEN %s AND gap_state='REVIEW_REQUIRED' THEN 0 ELSE 1 END,
                            observed_at DESC,response_id DESC LIMIT %s""",
                (actor.user_id,instance_id,route,route,key,key,open_only,open_only,open_only,limit,
                 actor.user_id,instance_id,open_only,open_only,open_only,open_only,
                 open_only,attention_only,attention_only,limit),
            )
            return tuple(RecallObservation.model_validate(row) for row in cur.fetchall())


def record_retrieval_feedback(connection: Connection,actor: TrustedActor,
                              command: RetrievalFeedbackCommand) -> RetrievalFeedbackResult:
    """Admit only a specific authenticated adjacent user feedback report.

    Ordinals refer to the actual persisted displayed passage order, not a
    new query or a model-generated ID. Feedback does not amend Memory.
    """
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not RetrievalFeedbackCommand:
        raise TypeError("validated retrieval feedback required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,actor,command.instance_id)
            receipt,prior=_receipt(cur,actor,command.instance_id,"memory.retrieval.feedback.v1",
                                   command,result_type=RetrievalFeedbackResult)
            if prior is not None:
                return prior
            _selection(cur,actor,command.instance_id,command.expected_generation)
            cur.execute(
                """SELECT o.route,o.outcome,o.evidence_refs,t.input_text
                   FROM memory_recall_observations o
                   JOIN conversation_responses r ON r.response_id=o.response_id
                     AND r.user_id=o.user_id AND r.instance_id=o.instance_id
                   JOIN conversation_turns original ON original.turn_id=o.turn_id
                     AND original.user_id=o.user_id AND original.instance_id=o.instance_id
                     AND original.conversation_id=o.conversation_id
                   JOIN conversation_turns t ON t.turn_id=%s AND t.user_id=o.user_id
                     AND t.instance_id=o.instance_id AND t.conversation_id=o.conversation_id
                     AND t.sequence=original.sequence+1
                   WHERE o.response_id=%s AND o.user_id=%s AND o.instance_id=%s
                     AND o.conversation_id=%s AND r.delivery_status='DELIVERED'
                     AND r.delivered_at<=t.created_at AND t.selection_generation=%s
                     AND r.selection_generation=t.selection_generation
                     AND NOT EXISTS (
                         SELECT 1 FROM conversation_assistant_followups f
                         WHERE f.user_id=t.user_id AND f.instance_id=t.instance_id
                           AND f.conversation_id=t.conversation_id
                           AND f.delivered_at>r.delivered_at AND f.delivered_at<=t.created_at
                     )
                   FOR SHARE OF r""",
                (command.feedback_turn_id,command.response_id,actor.user_id,command.instance_id,
                 command.conversation_id,command.expected_generation),
            )
            row=cur.fetchone()
            if row is None or row["input_text"].strip().casefold()!=feedback_phrase(command).casefold():
                raise PermissionDenied("feedback must match an authenticated adjacent report after delivery")
            if command.passage_ordinal is not None and (
                row["route"]!="KNOWLEDGE" or row["outcome"]!="AVAILABLE"
                or command.passage_ordinal>len(row["evidence_refs"])
            ):
                raise ValueError("feedback passage was not supplied")
            if command.outcome in ("RELEVANT","IRRELEVANT") and row["outcome"]!="AVAILABLE":
                raise ValueError("relevance feedback requires a supplied result")
            feedback_id=uuid4()
            event=_event(cur,actor,command.instance_id,feedback_id,1,"memory.retrieval.feedback.recorded.v1",
                         command.feedback_turn_id,receipt,aggregate_key="feedback_id")
            cur.execute(
                """INSERT INTO memory_retrieval_feedback(
                       feedback_id,user_id,instance_id,response_id,conversation_id,
                       feedback_turn_id,outcome,passage_ordinal,event_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING RETURNING feedback_id""",
                (feedback_id,actor.user_id,command.instance_id,command.response_id,command.conversation_id,
                 command.feedback_turn_id,command.outcome,command.passage_ordinal,event),
            )
            if cur.fetchone() is None:
                raise VersionConflict("feedback already recorded for that report or result")
            return _finish(cur,receipt,RetrievalFeedbackResult(feedback_id=feedback_id,event_id=event))
