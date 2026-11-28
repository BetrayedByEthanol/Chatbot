"""Read-only, source-linked post-session review of existing domain findings.

A caller selects a closed conversation range. This is not a session-end
trigger, a quality score, a new memory, or permission to revise objectives.
The snapshot is diagnostic; any later action requires its own owner check.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel,ConfigDict

from .contracts import TrustedActor
from .cognitive_lineage import reflection_lineage_current_sql
from .operation_contracts import require_idle
from .runtime import PermissionDenied


class SessionReviewNotReady(Exception):
    """The requested range contains unfinished or still-deliverable turns."""


class ReviewFinding(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    kind:Literal["EXPERIENCE","RECALL_GAP","RETRIEVAL_FEEDBACK","CORRECTION_SIGNAL",
                 "REFLECTION_PROPOSAL","GOAL_REPORT"]
    source_turn_id:UUID
    reference_id:UUID
    state:str
    related_id:UUID|None=None
    version:int|None=None
    owner_version:int|None=None
    report_kind:Literal["UNCLASSIFIED","PROGRESS","OBSTACLE","SUCCESS_REPORTED"]|None=None


class SessionReview(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    conversation_id:UUID
    start_sequence:int
    end_sequence:int
    observed_at:datetime
    cognitive_complete:bool
    summary_complete:bool
    eligible_turn_count:int
    feedback:Literal["NO_FEEDBACK","RELEVANCE_REPORTED","DISSATISFACTION_REPORTED","MIXED_REPORTED"]
    findings:tuple[ReviewFinding,...]
    truncated:bool
    # No aggregate evaluation of success or satisfaction from silence.


def review_session_range(connection,actor,instance_id,conversation_id,*,
                         end_sequence:int,start_sequence:int=1,limit:int=64)->SessionReview:
    """Trusted local review of <=64 explicitly selected closed input turns.

    Reuse the owner predicates for request/answer eligibility, goal evidence,
    reflection lineage and raw source validity. Feedback is an authenticated
    report about actual delivery, not proof the agent was right or wrong.
    All reads use one read-only database snapshot; no owner locks, writes,
    model calls or cross-session transcript scan.
    """
    require_idle(connection)
    if type(actor) is not TrustedActor or actor.application_session_id is not None:
        raise PermissionDenied("session review requires trusted local authenticated user")
    if not 1<=start_sequence<=end_sequence or end_sequence-start_sequence>=64 or not 1<=limit<=128:
        raise ValueError("invalid session review bounds")
    uid=actor.user_id
    scope=(uid,instance_id,conversation_id)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute("""SELECT c.next_sequence,c.cognitive_watermark,c.summary_watermark,
                              s.instance_id AS selected_instance,s.generation,
                              statement_timestamp() AS observed_at
                       FROM conversations c LEFT JOIN persona_selection s ON s.user_id=c.user_id
                       WHERE c.user_id=%s AND c.instance_id=%s AND c.conversation_id=%s""",scope)
        conv=cur.fetchone()
        if conv is None:
            raise PermissionDenied("conversation outside authenticated scope")
        if end_sequence>=conv["next_sequence"]:
            raise ValueError("review range extends beyond conversation")
        cur.execute("""SELECT t.turn_id,t.status,r.delivery_status,r.selection_generation,
                              CASE WHEN r.response_id IS NOT NULL THEN
                                response_lineage_current(r.response_id,t.user_id,t.instance_id)
                                ELSE false END AS response_current,
                              e.trust_level='AUTHENTICATED_USER'
                                AND e.event_type='conversation.user_text.received.v1'
                                AND workflow_source_current(t.user_id,t.instance_id,
                                                          t.conversation_id,t.turn_id,NULL) AS eligible
                       FROM conversation_turns t
                       LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
                         AND r.user_id=t.user_id AND r.instance_id=t.instance_id
                         AND r.conversation_id=t.conversation_id
                       LEFT JOIN events e ON e.event_id=t.source_input_event_id
                         AND e.user_id=t.user_id AND e.instance_id=t.instance_id
                       WHERE t.user_id=%s AND t.instance_id=%s AND t.conversation_id=%s
                         AND t.sequence BETWEEN %s AND %s
                       ORDER BY t.sequence""",(*scope,start_sequence,end_sequence))
        turns=cur.fetchall()
        if len(turns)!=end_sequence-start_sequence+1 or any(
            t["status"]=="PENDING" or (
                t["delivery_status"]=="GENERATED" and t["response_current"]
                and conv["selected_instance"]==instance_id
                and t["selection_generation"]==conv["generation"]
            ) for t in turns
        ):
            raise SessionReviewNotReady("review requires a closed contiguous range")
        ids=[t["turn_id"] for t in turns if t["eligible"]]
        findings=[]
        positive=negative=False
        if ids:
            # One bounded candidate query per owner. The source set is exact;
            # never report a different conversation's evidence as this session.
            cur.execute("""SELECT source_turn_id,source_event_id AS reference_id,
                                  CASE WHEN attention_until>clock_timestamp() THEN 'ACTIVE'
                                       ELSE 'AGED' END AS state
                           FROM memory_experience_markers WHERE user_id=%s AND instance_id=%s
                             AND conversation_id=%s AND source_turn_id=ANY(%s::uuid[])""",(*scope,ids))
            findings.extend(ReviewFinding(kind="EXPERIENCE",**row) for row in cur.fetchall())
            # Current unresolved recall, selected latest non-budget observation
            # before eligibility, just as MetaMemory does. A later resolution
            # in another session can close the gap without importing its text.
            cur.execute("""SELECT o.turn_id AS source_turn_id,o.response_id AS reference_id,
                              CASE WHEN EXISTS (
                                   SELECT 1 FROM memory_retrieval_feedback f
                                   WHERE f.response_id=o.response_id AND f.user_id=o.user_id
                                     AND f.instance_id=o.instance_id AND f.outcome IN ('INCORRECT','MISSED')
                                     AND workflow_source_current(f.user_id,f.instance_id,
                                                               f.conversation_id,f.feedback_turn_id,NULL)
                              ) THEN 'REVIEW_REQUIRED'
                              WHEN NOT memory_recall_response_current(o.response_id,o.user_id,
                                  o.instance_id,o.conversation_id,o.turn_id) THEN 'OPEN'
                              WHEN o.outcome='AVAILABLE' THEN 'RESOLVED'
                              WHEN o.outcome='AMBIGUOUS' AND o.clarification_key IS NOT NULL
                               AND memory_recall_choice_awaiting(o.user_id,o.instance_id,o.query_key,
                                                                 o.clarification_key,statement_timestamp())
                                THEN 'AWAITING_CLARIFICATION'
                              ELSE 'OPEN' END AS state
                           FROM memory_recall_observations o JOIN conversation_responses r
                             ON r.response_id=o.response_id AND r.user_id=o.user_id
                             AND r.instance_id=o.instance_id
                           WHERE o.user_id=%s AND o.instance_id=%s AND o.conversation_id=%s
                             AND o.turn_id=ANY(%s::uuid[]) AND o.outcome<>'BUDGET'
                             AND o.response_id=(
                                 SELECT newer.response_id FROM memory_recall_observations newer
                                 WHERE newer.user_id=o.user_id AND newer.instance_id=o.instance_id
                                   AND newer.route=o.route AND newer.query_key=o.query_key
                                   AND newer.outcome<>'BUDGET'
                                 ORDER BY newer.observed_at DESC,newer.response_id DESC LIMIT 1
                             )""",(*scope,ids))
            findings.extend(ReviewFinding(kind="RECALL_GAP",**row)
                            for row in cur.fetchall() if row["state"]!="RESOLVED")
            # An actual delivered result and its adjacent authenticated
            # feedback must both lie in the selected range. No feedback
            # inference from ordinary endings, repeated turns or self-rating.
            cur.execute("""SELECT f.feedback_turn_id AS source_turn_id,f.feedback_id AS reference_id,
                                  f.response_id AS related_id,f.outcome AS state
                           FROM memory_retrieval_feedback f
                           JOIN memory_recall_observations o ON o.response_id=f.response_id
                             AND o.user_id=f.user_id AND o.instance_id=f.instance_id
                             AND o.conversation_id=f.conversation_id
                           JOIN conversation_responses r ON r.response_id=f.response_id
                             AND r.user_id=f.user_id AND r.instance_id=f.instance_id
                           JOIN events e ON e.event_id=f.event_id AND e.user_id=f.user_id
                             AND e.instance_id=f.instance_id
                           WHERE f.user_id=%s AND f.instance_id=%s AND f.conversation_id=%s
                             AND f.feedback_turn_id=ANY(%s::uuid[]) AND o.turn_id=ANY(%s::uuid[])
                             AND r.delivery_status='DELIVERED'
                             AND e.trust_level='AUTHENTICATED_USER'""",(*scope,ids,ids))
            for row in cur.fetchall():
                findings.append(ReviewFinding(kind="RETRIEVAL_FEEDBACK",**row))
                positive|=row["state"]=="RELEVANT"
                negative|=row["state"] in ("IRRELEVANT","INCORRECT","MISSED")
            cur.execute("""SELECT signal.turn_id AS source_turn_id,signal.signal_id AS reference_id,
                                  'CORRECTION_PROPOSED' AS state
                           FROM conversation_intent_signals signal
                           WHERE signal.user_id=%s AND signal.instance_id=%s
                             AND signal.conversation_id=%s AND signal.turn_id=ANY(%s::uuid[])
                             AND signal.intent_kind='CORRECTION'
                             AND signal.epistemic_status='RULE_INFERRED'""",(*scope,ids))
            findings.extend(ReviewFinding(kind="CORRECTION_SIGNAL",**row) for row in cur.fetchall())
            cur.execute(f"""SELECT r.evidence_turn_id AS source_turn_id,r.reflection_id AS reference_id,
                                   r.kind AS state
                            FROM cognitive_reflections r WHERE r.user_id=%s AND r.instance_id=%s
                              AND r.conversation_id=%s AND r.evidence_turn_id=ANY(%s::uuid[])
                              AND r.kind IN ('QUESTION','HYPOTHESIS','SELF_EVALUATION')
                              AND r.review_status='PROPOSED' AND {reflection_lineage_current_sql()}""",
                        (*scope,ids))
            findings.extend(ReviewFinding(kind="REFLECTION_PROPOSAL",**row) for row in cur.fetchall())
            # Goal owner evidence is explicitly attached to a goal, not
            # semantic alignment guessed from discussion. For this pilot
            # only exact directly approved active roots qualify. Delegated
            # ancestry/other authority requires the existing ALIGN review.
            cur.execute("""SELECT e.source_turn_id,e.evidence_id AS reference_id,e.goal_id AS related_id,
                                  e.goal_version AS version,g.version AS owner_version,
                                  e.evidence_kind AS report_kind,
                                  CASE WHEN g.status='ACTIVE' AND g.parent_goal_id IS NULL
                                    AND g.creation_authority='USER_APPROVED'
                                    AND approval.trust_level='AUTHENTICATED_USER'
                                    AND approval.event_type='goal.created.v1'
                                    AND approval.payload->>'user_approved'='true'
                                    AND approval.payload->>'source_turn_id'=g.source_turn_id::text
                                    AND approval.payload->>'goal_id'=g.goal_id::text
                                    AND EXISTS (
                                        SELECT 1 FROM conversation_turns a
                                        WHERE a.user_id=g.user_id AND a.instance_id=g.instance_id
                                          AND a.conversation_id=g.source_conversation_id
                                          AND a.turn_id::text=approval.payload->>'approval_turn_id'
                                          AND workflow_source_current(g.user_id,g.instance_id,
                                              g.source_conversation_id,g.source_turn_id,a.turn_id)
                                    )
                                    THEN 'APPROVED_GOAL_REPORT' ELSE 'GOAL_REVIEW_REQUIRED' END AS state
                           FROM goal_evidence e JOIN goal_records g ON g.goal_id=e.goal_id
                             AND g.user_id=e.user_id AND g.instance_id=e.instance_id
                           JOIN events report ON report.event_id=e.event_id AND report.user_id=e.user_id
                             AND report.instance_id=e.instance_id
                           LEFT JOIN events approval ON approval.event_id=g.created_event_id
                             AND approval.user_id=g.user_id AND approval.instance_id=g.instance_id
                           WHERE e.user_id=%s AND e.instance_id=%s AND e.source_conversation_id=%s
                             AND e.source_turn_id=ANY(%s::uuid[])
                             AND goal_evidence_current(e.user_id,e.instance_id,e.goal_id,ARRAY[e.evidence_id])
                             AND report.trust_level='AUTHENTICATED_USER'
                             AND report.event_type='goal.evidence.recorded.v1'
                             AND report.payload->>'source_turn_id'=e.source_turn_id::text
                             AND report.payload->>'goal_id'=g.goal_id::text
                           ORDER BY array_position(%s::uuid[],e.source_turn_id),e.evidence_id
                           LIMIT %s""",(*scope,ids,ids,limit+1))
            findings.extend(ReviewFinding(kind="GOAL_REPORT",**row) for row in cur.fetchall())
        # Deterministic bounded result, no quality/ranking inference from
        # count or confidence. Owner feedback state includes all eligible
        # feedback even when individual findings are truncated.
        order={t["turn_id"]:i for i,t in enumerate(turns)}
        findings.sort(key=lambda f:(order[f.source_turn_id],f.kind,str(f.reference_id)))
        feedback=("MIXED_REPORTED" if positive and negative else
                  "RELEVANCE_REPORTED" if positive else
                  "DISSATISFACTION_REPORTED" if negative else "NO_FEEDBACK")
        return SessionReview(conversation_id=conversation_id,start_sequence=start_sequence,
            end_sequence=end_sequence,observed_at=conv["observed_at"],
            cognitive_complete=conv["cognitive_watermark"]>=end_sequence,
            summary_complete=conv["summary_watermark"]>=end_sequence,
            eligible_turn_count=len(ids),feedback=feedback,findings=tuple(findings[:limit]),
            truncated=len(findings)>limit)
