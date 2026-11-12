"""P3-01 domain-owned goal tree and strategy acceptance.

Only authenticated, selected Foreground user commands may create approved
goals, report progress or select a candidate strategy. A model reflection,
memory candidate or instruction delivered is not evidence of real-world
completion. Domain transactions use P0's receipts and transactional outbox.
"""

import json
from typing import Callable
from uuid import UUID,NAMESPACE_URL,uuid4,uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .goal_contracts import (
    CreateGoalCommand,EvidenceView,GoalCommandResult,GoalUserCommand,GoalView,
    ProposeGoalStrategyCommand,RecordGoalEvidenceCommand,
    SelectGoalStrategyCommand,SetGoalStatusCommand,StrategyView,
)
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict,PermissionDenied,StaleSelection,VersionConflict,
    _fingerprint,
)


_MAX_SUBGOAL_DEPTH=5
_MAX_CHILDREN=12


def _publish(cur,actor:TrustedActor,instance_id:UUID,kind:str,
             detail:dict,receipt_id:UUID)->UUID:
    """Goal-owned per-instance journal stream and P0 atomic outbox."""
    stream_id=uuid5(
        NAMESPACE_URL,
        f"persona-agent/goal/{actor.user_id}/{instance_id}",
    )
    cur.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES (%s,%s,%s,'goal')
           ON CONFLICT(stream_id) DO NOTHING""",
        (stream_id,actor.user_id,instance_id),
    )
    cur.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",
        (stream_id,actor.user_id,instance_id),
    )
    sequence=cur.fetchone()
    if sequence is None:
        raise PermissionDenied("goal event outside selected persona scope")
    event_id=uuid4()
    cur.execute(
        """INSERT INTO events(
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,correlation_id,
             payload
           ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb)""",
        (event_id,kind,actor.user_id,instance_id,stream_id,
         sequence["last_sequence"],actor.source_id,receipt_id,
         json.dumps(detail)),
    )
    cur.execute(
        "INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
        (uuid4(),event_id),
    )
    return event_id


def _selected(cur,actor:TrustedActor,command:GoalUserCommand)->None:
    # Persona switch uses FOR UPDATE. All goal changes require the same
    # selected Foreground generation through their entire short transaction.
    cur.execute(
        """SELECT instance_id,generation,mode FROM persona_selection
           WHERE user_id=%s FOR SHARE""",
        (actor.user_id,),
    )
    selected=cur.fetchone()
    if (selected is None or selected["instance_id"]!=command.instance_id
        or selected["generation"]!=command.expected_generation
        or selected["mode"]!="FOREGROUND"):
        raise StaleSelection("goal command requires selected Foreground persona")


def _source(cur,actor:TrustedActor,command:GoalUserCommand)->dict:
    cur.execute(
        """SELECT turn_id,input_text,created_at
           FROM conversation_turns
           WHERE conversation_id=%s AND turn_id=%s
             AND user_id=%s AND instance_id=%s""",
        (command.conversation_id,command.evidence_turn_id,
         actor.user_id,command.instance_id),
    )
    row=cur.fetchone()
    if row is None or command.evidence_quote.casefold() not in (
        row["input_text"].casefold()
    ):
        raise PermissionDenied("goal operation needs quoted authenticated user evidence")
    return row


def _goal(cur,actor:TrustedActor,instance_id:UUID,goal_id:UUID,
          *,lock:str="UPDATE")->dict:
    if lock not in ("UPDATE","SHARE"):
        raise ValueError("unsupported goal row-lock type")
    cur.execute(
        f"""SELECT * FROM goal_records
            WHERE goal_id=%s AND user_id=%s AND instance_id=%s
            FOR {lock}""",
        (goal_id,actor.user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("goal outside authenticated user/persona scope")
    return row


def _version(goal:dict,expected:int)->None:
    if goal["version"]!=expected:
        raise VersionConflict("goal version changed")


def _command(
    connection:Connection,actor:TrustedActor,command:GoalUserCommand,
    command_type:str,
    apply:Callable,
)->GoalCommandResult:
    require_idle(connection)
    _require_actor(actor)
    receipt_id=uuid4()
    fingerprint=_fingerprint(command)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("goal command outside user/persona scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,
                 command_type,command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json
                       FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,command_type,
                     command.idempotency_key),
                )
                prior=cur.fetchone()
                if (prior is None or prior["request_fingerprint"]!=fingerprint
                    or prior["result_json"] is None):
                    raise IdempotencyConflict("goal idempotency key reused differently")
                return GoalCommandResult.model_validate(
                    prior["result_json"]
                ).model_copy(update={"replayed":True})
            _selected(cur,actor,command)
            _source(cur,actor,command)
            result=apply(cur,actor,command,receipt_id)
            cur.execute(
                """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
                   WHERE receipt_id=%s""",
                (result.event_id,json.dumps(result.model_dump(mode="json")),
                 receipt_id),
            )
            return result


def create_goal(
    connection:Connection,actor:TrustedActor,command:CreateGoalCommand,
)->GoalCommandResult:
    """Create a root goal or an authorized bounded-depth subgoal.

    New subgoals must cite an expected parent version. Parent changes are
    serialized by their row lock, not by a global persona work lock.
    """
    def apply(cur,who,cmd,receipt):
        cur.execute("SELECT 1 FROM goal_records WHERE goal_id=%s",(cmd.goal_id,))
        if cur.fetchone() is not None:
            raise VersionConflict("goal identifier already used")
        depth=0
        if cmd.parent_goal_id is not None:
            parent=_goal(cur,who,cmd.instance_id,cmd.parent_goal_id)
            _version(parent,cmd.expected_parent_version)
            if parent["status"]!="ACTIVE":
                raise VersionConflict("cannot decompose a paused or abandoned goal")
            if parent["depth"]>=_MAX_SUBGOAL_DEPTH:
                raise VersionConflict("goal decomposition depth limit")
            cur.execute(
                """SELECT count(*) AS count FROM goal_records
                   WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s""",
                (cmd.parent_goal_id,who.user_id,cmd.instance_id),
            )
            if cur.fetchone()["count"]>=_MAX_CHILDREN:
                raise VersionConflict("goal has reached its subgoal budget")
            depth=parent["depth"]+1
            cur.execute(
                """UPDATE goal_records SET version=version+1,
                   updated_at=clock_timestamp() WHERE goal_id=%s
                     AND user_id=%s AND instance_id=%s""",
                (cmd.parent_goal_id,who.user_id,cmd.instance_id),
            )
        evt=_publish(
            cur,who,cmd.instance_id,"goal.created.v1",{
                "goal_id":str(cmd.goal_id),
                "parent_goal_id":str(cmd.parent_goal_id)
                  if cmd.parent_goal_id else None,
                "source_turn_id":str(cmd.evidence_turn_id),
                "user_approved":True,"depth":depth,
            },receipt,
        )
        cur.execute(
            """INSERT INTO goal_records(
                 goal_id,user_id,instance_id,parent_goal_id,depth,
                 title,desired_outcome,success_criterion,horizon,target_at,
                 evaluation_start_at,evaluation_end_at,
                 source_conversation_id,source_turn_id,source_quote,
                 created_event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cmd.goal_id,who.user_id,cmd.instance_id,cmd.parent_goal_id,
             depth,cmd.title.strip(),cmd.desired_outcome.strip(),
             cmd.success_criterion.strip(),cmd.horizon,cmd.target_at,
             cmd.evaluation_start_at,cmd.evaluation_end_at,
             cmd.conversation_id,cmd.evidence_turn_id,cmd.evidence_quote,
             evt),
        )
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=1,event_id=evt,
            action="GOAL_CREATED",entity_id=cmd.goal_id,
        )
    return _command(connection,actor,command,"goal.create.v1",apply)


def set_goal_status(
    connection:Connection,actor:TrustedActor,command:SetGoalStatusCommand,
)->GoalCommandResult:
    """Only the authenticated user can pause/resume/abandon a goal.

    No automatic ACHIEVED state exists: reported success is not verification.
    """
    def apply(cur,who,cmd,receipt):
        goal=_goal(cur,who,cmd.instance_id,cmd.goal_id)
        _version(goal,cmd.expected_goal_version)
        old=goal["status"]
        if (old=="ABANDONED" or old==cmd.new_status
            or (old=="ACTIVE" and cmd.new_status=="ACTIVE")
            or (old=="PAUSED" and cmd.new_status=="PAUSED")):
            raise VersionConflict("goal status transition is not actionable")
        version=goal["version"]+1
        evt=_publish(cur,who,cmd.instance_id,"goal.status.changed.v1",{
            "goal_id":str(cmd.goal_id),"old_status":old,
            "new_status":cmd.new_status,
            "source_turn_id":str(cmd.evidence_turn_id),"version":version,
        },receipt)
        cur.execute(
            """UPDATE goal_records SET status=%s,version=%s,
               updated_at=clock_timestamp()
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
            (cmd.new_status,version,cmd.goal_id,who.user_id,cmd.instance_id),
        )
        cur.execute(
            """INSERT INTO goal_status_revisions(
                 revision_id,goal_id,user_id,instance_id,goal_version,
                 old_status,new_status,source_conversation_id,source_turn_id,
                 source_quote,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),cmd.goal_id,who.user_id,cmd.instance_id,version,
             old,cmd.new_status,cmd.conversation_id,cmd.evidence_turn_id,
             cmd.evidence_quote,evt),
        )
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=version,event_id=evt,
            action="STATUS_CHANGED",entity_id=cmd.goal_id,
        )
    return _command(connection,actor,command,"goal.status.set.v1",apply)


def record_goal_evidence(
    connection:Connection,actor:TrustedActor,command:RecordGoalEvidenceCommand,
)->GoalCommandResult:
    """Append a user's outcome/obstacle report, never auto-verify a task."""
    def apply(cur,who,cmd,receipt):
        goal=_goal(cur,who,cmd.instance_id,cmd.goal_id)
        _version(goal,cmd.expected_goal_version)
        if goal["status"]!="ACTIVE":
            raise VersionConflict("cannot record live progress on paused/abandoned goal")
        cur.execute(
            """SELECT 1 FROM goal_evidence
               WHERE goal_id=%s AND source_turn_id=%s
                 AND evidence_kind=%s""",
            (cmd.goal_id,cmd.evidence_turn_id,cmd.evidence_kind),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("identical goal evidence already recorded")
        version=goal["version"]+1
        evt=_publish(cur,who,cmd.instance_id,"goal.evidence.recorded.v1",{
            "goal_id":str(cmd.goal_id),"evidence_id":str(cmd.evidence_id),
            "kind":cmd.evidence_kind,"epistemic_status":"USER_REPORTED",
            "source_turn_id":str(cmd.evidence_turn_id),
            "goal_version":version,
        },receipt)
        cur.execute(
            """INSERT INTO goal_evidence(
                 evidence_id,goal_id,user_id,instance_id,evidence_kind,
                 source_conversation_id,source_turn_id,source_quote,
                 goal_version,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cmd.evidence_id,cmd.goal_id,who.user_id,cmd.instance_id,
             cmd.evidence_kind,cmd.conversation_id,cmd.evidence_turn_id,
             cmd.evidence_quote,version,evt),
        )
        cur.execute(
            """UPDATE goal_records SET version=%s,updated_at=clock_timestamp()
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
            (version,cmd.goal_id,who.user_id,cmd.instance_id),
        )
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=version,event_id=evt,
            action="EVIDENCE_RECORDED",entity_id=cmd.evidence_id,
        )
    return _command(connection,actor,command,"goal.evidence.record.v1",apply)


def propose_goal_strategy(
    connection:Connection,actor:TrustedActor,command:ProposeGoalStrategyCommand,
)->GoalCommandResult:
    """Record a user-requested alternative; never change the active strategy."""
    def apply(cur,who,cmd,receipt):
        goal=_goal(cur,who,cmd.instance_id,cmd.goal_id,lock="SHARE")
        if goal["status"]!="ACTIVE":
            raise VersionConflict("goal not active for strategy proposals")
        cur.execute("SELECT 1 FROM goal_strategies WHERE strategy_id=%s",
                    (cmd.strategy_id,))
        if cur.fetchone() is not None:
            raise VersionConflict("strategy identifier already used")
        evt=_publish(cur,who,cmd.instance_id,"goal.strategy.proposed.v1",{
            "goal_id":str(cmd.goal_id),"strategy_id":str(cmd.strategy_id),
            "status":"CANDIDATE","source_turn_id":str(cmd.evidence_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO goal_strategies(
                 strategy_id,goal_id,user_id,instance_id,title,approach,
                 suitability_note,source_conversation_id,source_turn_id,
                 source_quote,created_event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cmd.strategy_id,cmd.goal_id,who.user_id,cmd.instance_id,
             cmd.title.strip(),cmd.approach.strip(),
             cmd.suitability_note.strip(),cmd.conversation_id,
             cmd.evidence_turn_id,cmd.evidence_quote,evt),
        )
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=goal["version"],event_id=evt,
            action="STRATEGY_PROPOSED",entity_id=cmd.strategy_id,
        )
    return _command(connection,actor,command,"goal.strategy.propose.v1",apply)


def select_goal_strategy(
    connection:Connection,actor:TrustedActor,command:SelectGoalStrategyCommand,
)->GoalCommandResult:
    """Select an approved candidate; append selection history, not a rewrite."""
    def apply(cur,who,cmd,receipt):
        goal=_goal(cur,who,cmd.instance_id,cmd.goal_id)
        _version(goal,cmd.expected_goal_version)
        if goal["status"]!="ACTIVE":
            raise VersionConflict("goal not active for strategy selection")
        cur.execute(
            """SELECT 1 FROM goal_strategies WHERE strategy_id=%s AND goal_id=%s
               AND user_id=%s AND instance_id=%s AND status='CANDIDATE'""",
            (cmd.strategy_id,cmd.goal_id,who.user_id,cmd.instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("candidate strategy outside approved goal scope")
        cur.execute(
            """SELECT strategy_id FROM goal_strategy_selections
               WHERE goal_id=%s ORDER BY goal_version DESC LIMIT 1""",
            (cmd.goal_id,),
        )
        current=cur.fetchone()
        if current is not None and current["strategy_id"]==cmd.strategy_id:
            raise VersionConflict("candidate already selected for this goal")
        version=goal["version"]+1
        evt=_publish(cur,who,cmd.instance_id,"goal.strategy.selected.v1",{
            "goal_id":str(cmd.goal_id),"strategy_id":str(cmd.strategy_id),
            "goal_version":version,"source_turn_id":str(cmd.evidence_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO goal_strategy_selections(
                 selection_id,goal_id,strategy_id,user_id,instance_id,
                 goal_version,source_conversation_id,source_turn_id,
                 source_quote,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),cmd.goal_id,cmd.strategy_id,who.user_id,cmd.instance_id,
             version,cmd.conversation_id,cmd.evidence_turn_id,
             cmd.evidence_quote,evt),
        )
        cur.execute(
            """UPDATE goal_records SET version=%s,updated_at=clock_timestamp()
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
            (version,cmd.goal_id,who.user_id,cmd.instance_id),
        )
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=version,event_id=evt,
            action="STRATEGY_SELECTED",entity_id=cmd.strategy_id,
        )
    return _command(connection,actor,command,"goal.strategy.select.v1",apply)


def list_goals(
    connection:Connection,actor:TrustedActor,instance_id:UUID,*,limit:int=120,
)->tuple[GoalView,...]:
    """Bounded read-only, scoped goal hierarchy with no inferred success."""
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=300:
        raise ValueError("invalid goal list bound")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
            (actor.user_id,instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("goal read outside authenticated persona scope")
        cur.execute(
            """SELECT g.goal_id,g.parent_goal_id,g.depth,g.title,
                      g.desired_outcome,g.success_criterion,g.horizon,
                      g.status,g.version,
                      (SELECT s.strategy_id FROM goal_strategy_selections s
                       WHERE s.goal_id=g.goal_id
                       ORDER BY s.goal_version DESC LIMIT 1)
                         AS selected_strategy_id,
                      (SELECT count(*) FROM goal_evidence e
                       WHERE e.goal_id=g.goal_id) AS evidence_count
               FROM goal_records g WHERE g.user_id=%s AND g.instance_id=%s
               ORDER BY g.depth,g.created_at,g.goal_id LIMIT %s""",
            (actor.user_id,instance_id,limit),
        )
        return tuple(GoalView.model_validate(dict(r)) for r in cur.fetchall())


def list_goal_strategies(
    connection:Connection,actor:TrustedActor,instance_id:UUID,goal_id:UUID,
    *,limit:int=30,
)->tuple[StrategyView,...]:
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=100:
        raise ValueError("invalid strategy list bound")
    with connection.cursor(row_factory=dict_row) as cur:
        _goal(cur,actor,instance_id,goal_id,lock="SHARE")
        cur.execute(
            """SELECT strategy_id,goal_id,title,approach,suitability_note,status
               FROM goal_strategies WHERE goal_id=%s AND user_id=%s AND instance_id=%s
               ORDER BY created_at,strategy_id LIMIT %s""",
            (goal_id,actor.user_id,instance_id,limit),
        )
        return tuple(StrategyView.model_validate(dict(r)) for r in cur.fetchall())


def list_goal_evidence(
    connection:Connection,actor:TrustedActor,instance_id:UUID,goal_id:UUID,
    *,limit:int=40,
)->tuple[EvidenceView,...]:
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=100:
        raise ValueError("invalid evidence list bound")
    with connection.cursor(row_factory=dict_row) as cur:
        _goal(cur,actor,instance_id,goal_id,lock="SHARE")
        cur.execute(
            """SELECT evidence_id,goal_id,evidence_kind AS kind,
                      epistemic_status,source_turn_id,source_quote
               FROM goal_evidence WHERE goal_id=%s AND user_id=%s AND instance_id=%s
               ORDER BY recorded_at DESC,evidence_id DESC LIMIT %s""",
            (goal_id,actor.user_id,instance_id,limit),
        )
        return tuple(EvidenceView.model_validate(dict(r)) for r in cur.fetchall())
