"""Goal-owned conversational candidate lifecycle.

Called only inside the authenticated Conversation ingress/response transaction.
Candidate rows are not goal_records and cannot authorize decomposition, tasks
or operations. The exact delivered proposal and separate user approval are
rechecked at the existing Goal command boundary.
"""
from datetime import timedelta
from uuid import NAMESPACE_URL,uuid5

from .goal_contracts import CreateGoalCommand
from .goal_conversation_contracts import parse_goal_dialogue,APPROVE,REJECT
from .runtime import PermissionDenied,VersionConflict

NO_PENDING="There is no current goal proposal to confirm. Please state the goal again."
MAX_REVISIONS=12


def render_candidate(row):
    outcome=row["desired_outcome"]
    criterion=row["success_criterion"]
    if criterion is None:
        return (f"Draft goal: {outcome}. Nothing is active yet. "
                "Tell me how to measure success, starting with 'For this goal, success means ...'.")
    return (f"Proposed goal: {outcome}. Success: {criterion}. "
            f"No end date is set. Reply '{APPROVE}' to activate it or '{REJECT}' to decline.")


def _publish_transition(cur,actor,row,turn_id,event_type,correlation_id):
    from .goal_state import _publish
    return _publish(cur,actor,row["instance_id"],event_type,{
        "goal_id":str(row["goal_id"]),"candidate_version":row["version"],
        "source_turn_id":str(row["source_turn_id"]),
        "proposal_turn_id":str(row["proposal_turn_id"]),
        "decision_turn_id":str(turn_id),
    },correlation_id)


def _transition(cur,actor,row,turn_id,event_id,status):
    event=_publish_transition(cur,actor,row,turn_id,
        "goal.conversation_candidate."+status.lower()+".v1",event_id)
    cur.execute(
        """UPDATE goal_conversation_candidates
           SET status=%s,decision_turn_id=%s,decision_event_id=%s
           WHERE goal_id=%s AND version=%s AND user_id=%s AND instance_id=%s
             AND status='PENDING' RETURNING goal_id""",
        (status,turn_id,event,row["goal_id"],row["version"],actor.user_id,row["instance_id"]),
    )
    if cur.fetchone() is None:
        raise VersionConflict("goal candidate changed")


def supersede_on_ingress(cur,actor,*,instance_id,conversation_id,turn_id,
                         sequence,generation,text,event_id):
    """Only the immediately following explicit decision/refinement may continue.

    A topic change, vague yes, generic desire, new request or intervening input
    retires the old challenge; no stale approval can be recovered by guessing.
    """
    action=parse_goal_dialogue(text)
    continuation=action is not None and action.kind in (
        "APPROVE","REJECT","REFINE","CRITERION",
    )
    cur.execute(
        """SELECT * FROM goal_conversation_candidates
           WHERE user_id=%s AND instance_id=%s AND conversation_id=%s AND status='PENDING'
             AND (NOT %s OR proposal_sequence<>%s OR selection_generation<>%s)
           FOR UPDATE""",
        (actor.user_id,instance_id,conversation_id,continuation,sequence-1,generation),
    )
    for row in cur.fetchall():
        _transition(cur,actor,row,turn_id,event_id,"SUPERSEDED")


def _adjacent(cur,actor,*,instance_id,conversation_id,turn_id,generation):
    """Only a currently deliverable, exactly rendered prior proposal qualifies."""
    cur.execute(
        """SELECT p.*,t.input_text AS current_text,t.source_input_event_id AS current_event_id,
                  r.response_text AS delivered_text,origin.created_at AS original_at,
                  t.created_at AS approval_at
           FROM goal_conversation_candidates p
           JOIN conversation_turns t ON t.turn_id=%s
             AND t.conversation_id=p.conversation_id AND t.user_id=p.user_id
             AND t.instance_id=p.instance_id AND t.sequence=p.proposal_sequence+1
           JOIN conversations conv ON conv.conversation_id=p.conversation_id
             AND conv.user_id=p.user_id AND conv.instance_id=p.instance_id
             AND conv.last_input_turn_id=t.turn_id
           JOIN conversation_turns origin ON origin.turn_id=p.source_turn_id
             AND origin.conversation_id=p.conversation_id AND origin.user_id=p.user_id
             AND origin.instance_id=p.instance_id
           JOIN conversation_responses r ON r.response_id=p.proposal_response_id
             AND r.turn_id=p.proposal_turn_id AND r.conversation_id=p.conversation_id
             AND r.user_id=p.user_id AND r.instance_id=p.instance_id
           WHERE p.user_id=%s AND p.instance_id=%s AND p.conversation_id=%s
             AND p.status='PENDING' AND p.selection_generation=%s
             AND t.selection_generation=p.selection_generation AND t.status='PENDING'
             AND r.selection_generation=p.selection_generation AND r.delivery_status='DELIVERED'
             AND r.delivered_at<=t.created_at AND origin.created_at<t.created_at
             AND t.created_at<=origin.created_at+interval '1 day'
             AND clock_timestamp()<=origin.created_at+interval '1 day'
             AND NOT EXISTS (
                 SELECT 1 FROM conversation_assistant_followups f
                 WHERE f.user_id=p.user_id AND f.instance_id=p.instance_id
                   AND f.conversation_id=p.conversation_id
                   AND f.delivered_at>r.delivered_at AND f.delivered_at<=t.created_at
             )
           FOR UPDATE OF p""",
        (turn_id,actor.user_id,instance_id,conversation_id,generation),
    )
    row=cur.fetchone()
    if row is None or row["delivered_text"]!=render_candidate(row):
        return None
    return row


def _command_for(row,approval_turn_id):
    return CreateGoalCommand(
        instance_id=row["instance_id"],expected_generation=row["selection_generation"],
        idempotency_key=f"conversation-goal:{row['goal_id']}:{row['version']}",
        conversation_id=row["conversation_id"],evidence_turn_id=row["source_turn_id"],
        evidence_quote=row["source_quote"],approval_turn_id=approval_turn_id,
        goal_id=row["goal_id"],title=row["title"],desired_outcome=row["desired_outcome"],
        success_criterion=row["success_criterion"],horizon=row["horizon"],
    )


def validate_conversational_approval(cur,actor,command,source):
    """Alternative proof for the existing Goal owner command, never an API flag.

    The exact pending payload, generation, latest adjacent delivered challenge,
    authenticated approval wording and expiry are all required.
    """
    if not isinstance(command,CreateGoalCommand) or command.parent_goal_id is not None:
        raise PermissionDenied("conversational approval only supports a root goal")
    if command.approval_turn_id is None or command.approval_turn_id==command.evidence_turn_id:
        raise PermissionDenied("a separate conversational approval is required")
    row=_adjacent(cur,actor,instance_id=command.instance_id,
        conversation_id=command.conversation_id,turn_id=command.approval_turn_id,
        generation=command.expected_generation)
    action=parse_goal_dialogue(row["current_text"]) if row else None
    if (row is None or row["success_criterion"] is None or action is None
            or action.kind!="APPROVE" or command!=_command_for(row,command.approval_turn_id)
            or source["input_text"]!=row["source_quote"]):
        raise PermissionDenied("goal approval does not match the delivered proposal")


def _retire_unusable(cur,actor,context,event_id):
    cur.execute(
        """SELECT * FROM goal_conversation_candidates
           WHERE user_id=%s AND instance_id=%s AND conversation_id=%s
             AND status='PENDING' FOR UPDATE""",
        (actor.user_id,context.instance_id,context.conversation_id),
    )
    for row in cur.fetchall():
        _transition(cur,actor,row,context.turn_id,event_id,"SUPERSEDED")


def apply_goal_dialogue(cur,actor,context,scoped,response_id,action):
    """Return canonical text; all mutations commit with the response.

    Conversation already holds the selected generation and latest-turn locks.
    No untrusted model proposal or generated confirmation is ever read here.
    """
    from .goal_state import _create_goal_from_conversation,_publish
    event_id=scoped["source_input_event_id"]
    prior=None
    if action.kind!="PROPOSE":
        prior=_adjacent(cur,actor,instance_id=context.instance_id,
            conversation_id=context.conversation_id,turn_id=context.turn_id,
            generation=context.selection_generation)
        if prior is None:
            _retire_unusable(cur,actor,context,event_id)
            return NO_PENDING
    if action.kind=="REJECT":
        _transition(cur,actor,prior,context.turn_id,event_id,"REJECTED")
        return "I won't activate that goal."
    if action.kind=="APPROVE":
        if prior["success_criterion"] is None:
            # Approval cannot substitute for missing success criteria.
            _transition(cur,actor,prior,context.turn_id,event_id,"SUPERSEDED")
            return "The goal needs a success criterion before approval. Please state the goal and how to measure success."
        command=_command_for(prior,context.turn_id)
        result=_create_goal_from_conversation(cur,actor,command)
        _transition(cur,actor,prior,context.turn_id,result.event_id,"APPROVED")
        return f"Goal approved: {prior['desired_outcome']}. It is now active; no task has been completed."
    if prior and prior["version"]>=MAX_REVISIONS:
        _transition(cur,actor,prior,context.turn_id,event_id,"SUPERSEDED")
        return "Please restate the complete goal and its success criterion as a new proposal."
    if prior:
        _transition(cur,actor,prior,context.turn_id,event_id,"SUPERSEDED")
    else:
        _retire_unusable(cur,actor,context,event_id)
    outcome=prior["desired_outcome"] if action.kind=="CRITERION" else action.outcome
    criterion=action.criterion
    source_turn=prior["source_turn_id"] if action.kind=="CRITERION" else context.turn_id
    source_quote=prior["source_quote"] if action.kind=="CRITERION" else scoped["input_text"]
    goal_id=prior["goal_id"] if prior else uuid5(NAMESPACE_URL,f"persona-agent/goal-candidate/{context.turn_id}")
    version=prior["version"]+1 if prior else 1
    row={
        "goal_id":goal_id,"version":version,"instance_id":context.instance_id,
        "source_turn_id":source_turn,"proposal_turn_id":context.turn_id,
        "desired_outcome":outcome,"success_criterion":criterion,
    }
    created=_publish(cur,actor,context.instance_id,"goal.conversation_candidate.proposed.v1",{
        "goal_id":str(goal_id),"candidate_version":version,
        "source_turn_id":str(source_turn),"proposal_turn_id":str(context.turn_id),
        "proposal_response_id":str(response_id),"has_success_criterion":criterion is not None,
    },event_id)
    cur.execute(
        """INSERT INTO goal_conversation_candidates(
               goal_id,version,user_id,instance_id,conversation_id,selection_generation,
               source_turn_id,source_quote,proposal_turn_id,proposal_response_id,proposal_sequence,
               title,desired_outcome,success_criterion,horizon,created_event_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)""",
        (goal_id,version,actor.user_id,context.instance_id,context.conversation_id,
         context.selection_generation,source_turn,source_quote,context.turn_id,response_id,
         scoped["sequence"],outcome,outcome,criterion,created),
    )
    return render_candidate(row)


def list_goal_candidates(connection,actor,instance_id,*,limit=60):
    """Read scoped proposal revisions without reactivating stale challenges."""
    from psycopg.rows import dict_row
    from .conversation import _require_actor
    from .goal_state import _read_selected
    from .operation_contracts import require_idle
    from .goal_conversation_contracts import GoalCandidateView
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=120:
        raise ValueError("invalid candidate list limit")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT p.goal_id,p.version,p.conversation_id,p.selection_generation,
                          p.source_turn_id,p.proposal_turn_id,p.proposal_response_id,
                          p.desired_outcome,p.success_criterion,p.horizon,p.status,p.decision_turn_id,
                          (p.status='PENDING' AND p.success_criterion IS NOT NULL
                           AND s.mode='FOREGROUND' AND s.generation=p.selection_generation
                           AND c.last_input_turn_id=p.proposal_turn_id
                           AND r.delivery_status='DELIVERED'
                           AND clock_timestamp()<=origin.created_at+interval '1 day'
                           AND NOT EXISTS (
                               SELECT 1 FROM conversation_assistant_followups f
                               WHERE f.user_id=p.user_id AND f.instance_id=p.instance_id
                                 AND f.conversation_id=p.conversation_id
                                 AND f.delivered_at>r.delivered_at
                           )) AS approval_available
                   FROM goal_conversation_candidates p
                   JOIN persona_selection s ON s.user_id=p.user_id AND s.instance_id=p.instance_id
                   JOIN conversations c ON c.conversation_id=p.conversation_id
                       AND c.user_id=p.user_id AND c.instance_id=p.instance_id
                   JOIN conversation_turns origin ON origin.turn_id=p.source_turn_id
                       AND origin.conversation_id=p.conversation_id AND origin.user_id=p.user_id
                       AND origin.instance_id=p.instance_id
                   JOIN conversation_responses r ON r.response_id=p.proposal_response_id
                       AND r.user_id=p.user_id AND r.instance_id=p.instance_id
                   WHERE p.user_id=%s AND p.instance_id=%s
                   ORDER BY p.created_at DESC,p.goal_id,p.version DESC LIMIT %s""",
                (actor.user_id,instance_id,limit),
            )
            return tuple(GoalCandidateView.model_validate(row) for row in cur.fetchall())
