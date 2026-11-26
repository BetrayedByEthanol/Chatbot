"""P3-01 domain-owned goal tree and strategy acceptance.

Authenticated user commands create approved roots; a narrowly trusted
platform command may derive an evidence-checked child within that authority.
Only authenticated, selected Foreground user commands may create approved
goals, report progress or select a candidate strategy. A model reflection,
memory candidate or instruction delivered is not evidence of real-world
completion. Domain transactions use P0's receipts and transactional outbox.
"""

import json
from datetime import timedelta
from typing import Callable
from uuid import UUID,NAMESPACE_URL,uuid4,uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .goal_delegation_contracts import (
    DeriveWeeklyWalkingMilestoneCommand, weekly_walk_target, milestone_fields,
)
from .conversation import _require_actor
from .goal_contracts import (
    CreateGoalCommand,EvidenceView,GoalCommandResult,GoalUserCommand,GoalView,
    ProposeGoalStrategyCommand,ProposeResearchStrategyCommand,RecordGoalEvidenceCommand,
    SelectGoalStrategyCommand,SetGoalStatusCommand,StrategyView,
    goal_action_approval_phrase,
)
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict,PermissionDenied,StaleSelection,VersionConflict,
    _fingerprint,
)


_MAX_SUBGOAL_DEPTH=5
_MAX_CHILDREN=12


def _publish(cur,actor:TrustedActor | TrustedPlatformActor,instance_id:UUID,kind:str,
             detail:dict,receipt_id:UUID)->UUID:
    """Goal-owned per-instance journal stream and P0 atomic outbox."""
    trust = ("TRUSTED_PLATFORM" if type(actor) is TrustedPlatformActor
             else "AUTHENTICATED_USER")
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
           ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (event_id,kind,actor.user_id,instance_id,stream_id,
         sequence["last_sequence"],actor.source_id,trust,receipt_id,
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


def _require_goal_approval(cur,actor:TrustedActor,command:GoalUserCommand,
                           source:dict)->None:
    """Reject goal state changes that lack a distinct exact user confirmation."""
    if isinstance(command,CreateGoalCommand):
        cur.execute(
            """SELECT 1 FROM goal_conversation_candidates
               WHERE goal_id=%s LIMIT 1""",
            (command.goal_id,),
        )
        if cur.fetchone() is not None:
            raise PermissionDenied("conversational candidate requires its delivered approval route")
    approval_id=command.approval_turn_id
    if approval_id is None or approval_id==command.evidence_turn_id:
        raise PermissionDenied("goal change requires a separate user confirmation")
    cur.execute(
        """SELECT input_text,created_at FROM conversation_turns
           WHERE conversation_id=%s AND turn_id=%s
             AND user_id=%s AND instance_id=%s""",
        (command.conversation_id,approval_id,
         actor.user_id,command.instance_id),
    )
    confirmation=cur.fetchone()
    if (confirmation is None
        or confirmation["input_text"].strip()!=goal_action_approval_phrase(command)
        or confirmation["created_at"]<source["created_at"]
        or confirmation["created_at"]>source["created_at"]+timedelta(days=1)):
        raise PermissionDenied("goal approval does not match the exact action")


def _read_selected(cur,actor:TrustedActor,instance_id:UUID)->None:
    """Persona context must not read private state of a nonselected instance."""
    cur.execute(
        "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
        (actor.user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("goal read outside authenticated persona scope")
    cur.execute(
        "SELECT instance_id FROM persona_selection WHERE user_id=%s FOR SHARE",
        (actor.user_id,),
    )
    selected=cur.fetchone()
    if selected is None or selected["instance_id"]!=instance_id:
        raise StaleSelection("private goal read requires the selected persona")


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


def _command_in_transaction(
    cur,actor:TrustedActor,command:GoalUserCommand,command_type:str,
    apply:Callable,*,conversational:bool=False,
)->GoalCommandResult:
    _require_actor(actor)
    receipt_id=uuid4()
    fingerprint=_fingerprint(command)
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
    source=_source(cur,actor,command)
    if isinstance(command,(
        CreateGoalCommand,SetGoalStatusCommand,
        SelectGoalStrategyCommand,ProposeResearchStrategyCommand,
    )):
        if conversational:
            from .goal_conversation import validate_conversational_approval
            validate_conversational_approval(cur,actor,command,source)
        else:
            _require_goal_approval(cur,actor,command,source)
    result=apply(cur,actor,command,receipt_id)
    cur.execute(
        """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
           WHERE receipt_id=%s""",
        (result.event_id,json.dumps(result.model_dump(mode="json")),
         receipt_id),
    )
    return result



def _command(
    connection:Connection,actor:TrustedActor,command:GoalUserCommand,
    command_type:str,apply:Callable,
)->GoalCommandResult:
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            return _command_in_transaction(cur,actor,command,command_type,apply)


def _create_goal_apply(cur,who,cmd,receipt):
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
            "approval_turn_id":str(cmd.approval_turn_id),
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

def create_goal(
    connection:Connection,actor:TrustedActor,command:CreateGoalCommand,
)->GoalCommandResult:
    """Create an explicitly authorized root or bounded-depth subgoal."""
    return _command(connection,actor,command,"goal.create.v1",_create_goal_apply)


def _create_goal_from_conversation(cur,actor:TrustedActor,command:CreateGoalCommand)->GoalCommandResult:
    """Internal owner entry: exact delivered candidate authorization is mandatory."""
    return _command_in_transaction(cur,actor,command,"goal.create.conversational.v1",_create_goal_apply,
                                   conversational=True)



def _locked_active_goal_ancestry(
    cur, actor: TrustedPlatformActor, instance_id: UUID, parent_id: UUID,
) -> dict:
    """Fence the entire delegation chain against a concurrent status change.

    Read only the ancestry IDs first, then lock every row in UUID order,
    matching the stable order used by Task._active_goal_chain. The immediate
    parent takes FOR UPDATE (for its later version bump), and ancestors take
    FOR SHARE (for status stability). Never lock the immediate parent first:
    doing so reverses the order used by task admission and can deadlock.
    No model or external work happens while these locks are held.
    """
    cur.execute(
        """WITH RECURSIVE ancestry AS (
               SELECT goal_id,parent_goal_id,depth,
                      ARRAY[goal_id]::uuid[] AS path
               FROM goal_records
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s
               UNION ALL
               SELECT g.goal_id,g.parent_goal_id,g.depth,
                      a.path || g.goal_id
               FROM goal_records g
               JOIN ancestry a ON g.goal_id=a.parent_goal_id
               WHERE g.user_id=%s AND g.instance_id=%s
                 AND NOT g.goal_id=ANY(a.path)
                 AND cardinality(a.path)<=%s
           )
           SELECT goal_id,parent_goal_id,depth FROM ancestry""",
        (parent_id,actor.user_id,instance_id,actor.user_id,instance_id,
         _MAX_SUBGOAL_DEPTH + 1),
    )
    ancestry=cur.fetchall()
    if not ancestry:
        raise PermissionDenied("delegation parent outside user/persona scope")
    if (len(ancestry)>_MAX_SUBGOAL_DEPTH+1
        or len({x["goal_id"] for x in ancestry})!=len(ancestry)
        or ancestry[0]["goal_id"]!=parent_id
        or ancestry[-1]["parent_goal_id"] is not None):
        raise VersionConflict("invalid or incomplete goal ancestry")

    # Every observed link must still exist after locks are acquired.
    locked={}
    for goal_id in sorted((x["goal_id"] for x in ancestry)):
        locked[goal_id]=_goal(
            cur,actor,instance_id,goal_id,
            lock="UPDATE" if goal_id==parent_id else "SHARE",
        )
    for index,observed in enumerate(ancestry):
        row=locked[observed["goal_id"]]
        if (row["parent_goal_id"]!=observed["parent_goal_id"]
            or row["depth"]!=observed["depth"]
            or row["status"]!="ACTIVE"
            or row["depth"]!=len(ancestry)-index-1
            or (index+1<len(ancestry) and
                row["parent_goal_id"]!=ancestry[index+1]["goal_id"])):
            raise VersionConflict("goal or ancestor paused, abandoned or changed")
    return locked[parent_id]


def _has_existing_goal_tasks(cur,actor,instance_id,goal_id):
    """Lazy expansion stops at existing work, including uncertain evidence.

    The caller holds the parent Goal lock. Task creation increments that
    revision; cancelled tasks alone do not constitute an active plan.
    """
    cur.execute(
        """SELECT 1 FROM goal_tasks WHERE goal_id=%s AND user_id=%s
             AND instance_id=%s AND status<>'CANCELLED' LIMIT 1""",
        (goal_id,actor.user_id,instance_id),
    )
    return cur.fetchone() is not None


def derive_weekly_walking_milestone(
    connection: Connection,
    actor: TrustedPlatformActor,
    command: DeriveWeeklyWalkingMilestoneCommand,
) -> GoalCommandResult:
    """Create a bounded quantitative child, not a new primary objective.

    The already approved parent supplies authority. Both its outcome and
    success criterion must express the SAME weekly walking target; the
    derived child must be strictly smaller. This intentionally abstains for
    every other goal form: free-text model claims cannot prove contribution.
    Only the trusted local Goal evaluation component may call this entry
    point; Background autonomy has a separate readiness/authorization gate.
    """
    require_idle(connection)
    if type(actor) is not TrustedPlatformActor or actor.source_id != "goal.delegation.v1":
        raise PermissionDenied("derived subgoal requires trusted Goal evaluator")
    if command.goal_id == command.parent_goal_id:
        raise ValueError("a goal cannot be its own subgoal")

    receipt_id = uuid4()
    command_type = "goal.subgoal.derive_weekly_walk.v1"
    fingerprint = _fingerprint(command)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("delegation instance outside user/persona scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,command_type,
                 command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,command_type,
                     command.idempotency_key),
                )
                old = cur.fetchone()
                if (old is None or old["request_fingerprint"] != fingerprint
                    or old["result_json"] is None):
                    raise IdempotencyConflict("delegation key reused differently")
                return GoalCommandResult.model_validate(
                    old["result_json"]
                ).model_copy(update={"replayed":True})

            _selected(cur,actor,command)
            parent = _locked_active_goal_ancestry(
                cur,actor,command.instance_id,command.parent_goal_id,
            )
            _version(parent,command.expected_parent_version)
            if parent["depth"] >= _MAX_SUBGOAL_DEPTH:
                raise VersionConflict("goal decomposition depth limit")
            if _has_existing_goal_tasks(cur,actor,command.instance_id,command.parent_goal_id):
                raise VersionConflict("goal decomposition stopped at existing tasks")

            # A platform-derived child may extend an earlier validated
            # delegation; a direct parent must have authentic approval.
            cur.execute(
                """SELECT event_type,trust_level,payload FROM events
                   WHERE event_id=%s AND user_id=%s AND instance_id=%s""",
                (parent["created_event_id"],actor.user_id,command.instance_id),
            )
            origin = cur.fetchone()
            if origin is None or origin["event_type"] != "goal.created.v1":
                raise PermissionDenied("parent has no authenticated goal provenance")
            if parent["creation_authority"] == "USER_APPROVED":
                valid = (origin["trust_level"] == "AUTHENTICATED_USER"
                         and origin["payload"].get("user_approved") is True
                         and origin["payload"].get("approval_turn_id") is not None)
            else:
                valid = (
                    parent["creation_authority"] in ("DELEGATED_MILESTONE", "DELEGATED_VERIFIED", "DELEGATED_REVIEWED")
                    and origin["trust_level"] == "TRUSTED_PLATFORM"
                    and origin["payload"].get("authority") == "DELEGATED_PARENT"
                )
            if not valid:
                raise PermissionDenied("parent creation did not grant delegation")

            parent_target = weekly_walk_target(
                parent["desired_outcome"],parent["success_criterion"],
            )
            if parent_target is None or command.weekly_walks >= parent_target:
                raise PermissionDenied("unproven or noncontributing child milestone")
            title,outcome,criterion = milestone_fields(command.weekly_walks)
            from .goal_decomposition_contracts import verified_count_measure
            cur.execute(
                """SELECT desired_outcome,success_criterion FROM goal_records
                   WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s""",
                (command.parent_goal_id,actor.user_id,command.instance_id),
            )
            if any(verified_count_measure(row["desired_outcome"],row["success_criterion"])
                   ==("complete","walk","week",command.weekly_walks)
                   for row in cur.fetchall()):
                raise VersionConflict("equivalent child milestone already exists")
            cur.execute(
                """SELECT count(*) AS n FROM goal_records
                   WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s""",
                (command.parent_goal_id,actor.user_id,command.instance_id),
            )
            if cur.fetchone()["n"] >= _MAX_CHILDREN:
                raise VersionConflict("goal has reached its subgoal budget")
            cur.execute("SELECT 1 FROM goal_records WHERE goal_id=%s",
                        (command.goal_id,))
            if cur.fetchone() is not None:
                raise VersionConflict("goal identifier already used")

            event_id = _publish(
                cur,actor,command.instance_id,"goal.created.v1",{
                    "goal_id":str(command.goal_id),
                    "parent_goal_id":str(command.parent_goal_id),
                    "parent_created_event_id":str(parent["created_event_id"]),
                    "parent_version":parent["version"],
                    "source_turn_id":str(parent["source_turn_id"]),
                    "source_goal_id":str(command.parent_goal_id),
                    "authority":"DELEGATED_PARENT",
                    "user_approved":False,
                    "contribution_kind":"QUANTITATIVE_WEEKLY_MILESTONE",
                    "method_revision":"mvp03a1.weekly_walking.1",
                    "parent_weekly_walks":parent_target,
                    "child_weekly_walks":command.weekly_walks,
                    "depth":parent["depth"]+1,
                },receipt_id,
            )
            cur.execute(
                """INSERT INTO goal_records(
                     goal_id,user_id,instance_id,parent_goal_id,depth,
                     title,desired_outcome,success_criterion,horizon,
                     source_conversation_id,source_turn_id,source_quote,
                     created_event_id,creation_authority
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SHORT',%s,%s,%s,%s,
                             'DELEGATED_MILESTONE')""",
                (command.goal_id,actor.user_id,command.instance_id,
                 command.parent_goal_id,parent["depth"]+1,title,outcome,
                 criterion,parent["source_conversation_id"],
                 parent["source_turn_id"],parent["source_quote"],event_id),
            )
            cur.execute(
                """UPDATE goal_records SET version=version+1,
                     updated_at=clock_timestamp() WHERE goal_id=%s
                     AND user_id=%s AND instance_id=%s""",
                (command.parent_goal_id,actor.user_id,command.instance_id),
            )
            result = GoalCommandResult(
                goal_id=command.goal_id,goal_version=1,event_id=event_id,
                action="GOAL_CREATED",entity_id=command.goal_id,
            )
            cur.execute(
                """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
                   WHERE receipt_id=%s""",
                (event_id,result.model_dump_json(),receipt_id),
            )
            return result


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
            "approval_turn_id":str(cmd.approval_turn_id),
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
            "classification_status":"UNREVIEWED",
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
        researched=isinstance(cmd,ProposeResearchStrategyCommand)
        if researched:
            goal=_locked_active_goal_ancestry(cur,who,cmd.instance_id,cmd.goal_id)
            from .strategy_research import validate_research_candidate
            validate_research_candidate(cur,who,goal,cmd)
        else:
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
        if researched:
            from .strategy_research import save_research_links
            save_research_links(cur,who,cmd,evt)
        return GoalCommandResult(
            goal_id=cmd.goal_id,goal_version=goal["version"],event_id=evt,
            action="STRATEGY_PROPOSED",entity_id=cmd.strategy_id,
        )
    return _command(connection,actor,command,
        "goal.strategy.propose.research.v1" if isinstance(command,ProposeResearchStrategyCommand)
        else "goal.strategy.propose.v1",apply)


def select_goal_strategy(
    connection:Connection,actor:TrustedActor,command:SelectGoalStrategyCommand,
)->GoalCommandResult:
    """Select an approved candidate; append selection history, not a rewrite."""
    def apply(cur,who,cmd,receipt):
        from .strategy_research import research_record,validate_research_selection
        research=research_record(cur,who,cmd.instance_id,cmd.goal_id,cmd.strategy_id)
        goal=(_locked_active_goal_ancestry(cur,who,cmd.instance_id,cmd.goal_id)
              if research else _goal(cur,who,cmd.instance_id,cmd.goal_id))
        _version(goal,cmd.expected_goal_version)
        if goal["status"]!="ACTIVE":
            raise VersionConflict("goal not active for strategy selection")
        if research:
            validate_research_selection(cur,who,cmd,research)
        elif research_record(cur,who,cmd.instance_id,cmd.goal_id,cmd.strategy_id):
            # A researched candidate committed while we waited for the
            # goal lock. Retry to acquire its full ancestry in stable order.
            raise VersionConflict("research strategy changed before selection")
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
            "approval_turn_id":str(cmd.approval_turn_id),
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
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT g.goal_id,g.parent_goal_id,g.depth,g.title,
                          g.desired_outcome,g.success_criterion,g.horizon,
                          g.status,g.version,g.creation_authority,
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
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
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
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            _goal(cur,actor,instance_id,goal_id,lock="SHARE")
            cur.execute(
                """SELECT evidence_id,goal_id,evidence_kind AS kind,
                          epistemic_status,classification_status,
                          source_turn_id,source_quote
                   FROM goal_evidence WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                   ORDER BY recorded_at DESC,evidence_id DESC LIMIT %s""",
                (goal_id,actor.user_id,instance_id,limit),
            )
            return tuple(EvidenceView.model_validate(dict(r)) for r in cur.fetchall())
