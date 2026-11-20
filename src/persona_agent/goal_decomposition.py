"""MVP-03A.2: generalized Goal-owned subgoal proposal admission.

A proposal is a suggestion, not an authorization. The existing Goal owner
accepts only an independently checkable count milestone without new user
approval. Other proposals are durably staged for future semantic review,
never made actionable merely because a model supplied a rationale.
"""
import json
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .goal_decomposition_contracts import (
    DerivedSubgoalProposalResult,DerivedSubgoalProposalView,ProposeDerivedSubgoalCommand,
    proven_count_milestone,proposal_digest,
)
from .goal_state import (
    _MAX_CHILDREN,_MAX_SUBGOAL_DEPTH,_locked_active_goal_ancestry,
    _read_selected,
    _publish,_selected,
)
from .lifecycle import TrustedPlatformActor
from .contracts import TrustedActor
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict,PermissionDenied,VersionConflict,_fingerprint,
)


def _authorized_lineage(cur,actor,instance_id,parent):
    """Check signed source-chain provenance while ancestry is row-locked.

    Event provenance is independently inspected for every chain member;
    a model claim referring to an approved ancestor is not a delegation.
    """
    cur.execute(
        """WITH RECURSIVE chain AS (
             SELECT goal_id,parent_goal_id,created_event_id,
                    creation_authority,0 AS distance
             FROM goal_records WHERE user_id=%s AND instance_id=%s
                AND goal_id=%s
             UNION ALL
             SELECT g.goal_id,g.parent_goal_id,g.created_event_id,
                    g.creation_authority,c.distance+1
             FROM goal_records g JOIN chain c ON g.goal_id=c.parent_goal_id
             WHERE g.user_id=%s AND g.instance_id=%s AND c.distance<%s
           )
           SELECT c.*,e.event_type,e.trust_level,e.payload
           FROM chain c LEFT JOIN events e ON e.event_id=c.created_event_id
           ORDER BY c.distance""",
        (actor.user_id,instance_id,parent["goal_id"],
         actor.user_id,instance_id,_MAX_SUBGOAL_DEPTH),
    )
    rows=cur.fetchall()
    if not rows or rows[-1]["parent_goal_id"] is not None:
        raise PermissionDenied("unverified goal ancestry")
    if len(rows)>_MAX_SUBGOAL_DEPTH+1:
        raise PermissionDenied("unbounded goal ancestry")
    for i,row in enumerate(rows):
        if row["event_type"]!="goal.created.v1":
            raise PermissionDenied("goal origin is not authoritative")
        payload=row["payload"] or {}
        if payload.get("goal_id")!=str(row["goal_id"]):
            raise PermissionDenied("goal event identity mismatch")
        if row["creation_authority"]=="USER_APPROVED":
            valid=(row["trust_level"]=="AUTHENTICATED_USER"
                   and payload.get("user_approved") is True
                   and payload.get("approval_turn_id") is not None)
        else:
            valid=(row["creation_authority"] in (
                       "DELEGATED_MILESTONE","DELEGATED_VERIFIED")
                   and row["trust_level"]=="TRUSTED_PLATFORM"
                   and payload.get("authority")=="DELEGATED_PARENT"
                   and payload.get("parent_goal_id")==str(row["parent_goal_id"]))
            if valid and i+1<len(rows):
                valid=(payload.get("parent_created_event_id")
                       ==str(rows[i+1]["created_event_id"]))
        if not valid:
            raise PermissionDenied("goal ancestry lacks delegated authorization")


def propose_derived_subgoal(
    connection:Connection,actor:TrustedPlatformActor,
    command:ProposeDerivedSubgoalCommand,
)->DerivedSubgoalProposalResult:
    """Stage, or auto-accept a *proved* quantitative child, in one transaction.

    This entry point is for trusted Goal evaluation only. P2 Background
    admission and operation authorization are intentionally not exposed here.
    """
    require_idle(connection)
    if (type(actor) is not TrustedPlatformActor
        or actor.source_id!="goal.decomposition.v1"):
        raise PermissionDenied("subgoal proposal requires trusted Goal evaluator")
    receipt_id=uuid4()
    command_type="goal.subgoal.propose.v1"
    fingerprint=_fingerprint(command)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("proposal outside selected user/persona")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,command_type,
                 command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s AND command_type=%s
                         AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,command_type,
                     command.idempotency_key),
                )
                saved=cur.fetchone()
                if (saved is None or saved["request_fingerprint"]!=fingerprint
                    or saved["result_json"] is None):
                    raise IdempotencyConflict("subgoal proposal key reused differently")
                return DerivedSubgoalProposalResult.model_validate(
                    saved["result_json"]
                ).model_copy(update={"replayed":True})

            _selected(cur,actor,command)
            parent=_locked_active_goal_ancestry(
                cur,actor,command.instance_id,command.parent_goal_id,
            )
            if parent["version"]!=command.expected_parent_version:
                raise VersionConflict("parent version changed")
            if parent["depth"]>=_MAX_SUBGOAL_DEPTH:
                raise VersionConflict("goal decomposition depth limit")
            if parent["created_event_id"]!=command.source_parent_event_id:
                raise PermissionDenied("stale or forged parent source event")
            _authorized_lineage(cur,actor,command.instance_id,parent)

            cur.execute("SELECT 1 FROM goal_records WHERE goal_id=%s",
                        (command.proposed_goal_id,))
            if cur.fetchone() is not None:
                raise VersionConflict("proposed goal identifier already exists")
            cur.execute(
                """SELECT count(*) AS n FROM goal_records
                   WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s""",
                (command.parent_goal_id,actor.user_id,command.instance_id),
            )
            if cur.fetchone()["n"]>=_MAX_CHILDREN:
                raise VersionConflict("parent has reached its child budget")
            cur.execute(
                """SELECT 1 FROM goal_records
                   WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s
                     AND lower(desired_outcome)=lower(%s)
                     AND lower(success_criterion)=lower(%s) LIMIT 1""",
                (command.parent_goal_id,actor.user_id,command.instance_id,
                 command.desired_outcome.strip(),command.success_criterion.strip()),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("equivalent subgoal already exists")

            checked=(len(command.desired_outcome.strip())<=120
                     and proven_count_milestone(
                         parent["desired_outcome"],parent["success_criterion"],
                         command.desired_outcome,command.success_criterion,
                     ))
            if not checked:
                # Prevent unbounded model candidate accumulation.
                cur.execute(
                    """SELECT count(*) AS n FROM goal_subgoal_proposals
                       WHERE parent_goal_id=%s AND user_id=%s
                         AND instance_id=%s AND status='PENDING_REVIEW'""",
                    (command.parent_goal_id,actor.user_id,command.instance_id),
                )
                if cur.fetchone()["n"]>=24:
                    raise VersionConflict("subgoal review candidate budget reached")

            digest=proposal_digest(command)
            cur.execute(
                """SELECT 1 FROM goal_subgoal_proposals
                   WHERE user_id=%s AND instance_id=%s AND parent_goal_id=%s
                     AND proposal_digest=%s""",
                (actor.user_id,command.instance_id,command.parent_goal_id,digest),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("duplicate subgoal proposal")

            method=("DETERMINISTIC_COUNT_MILESTONE" if checked
                    else "REVIEW_REQUIRED")
            status="ACCEPTED" if checked else "PENDING_REVIEW"
            proposal_event=_publish(
                cur,actor,command.instance_id,"goal.subgoal.proposed.v1",{
                    "proposal_id":str(command.proposal_id),
                    "proposed_goal_id":str(command.proposed_goal_id),
                    "parent_goal_id":str(command.parent_goal_id),
                    "parent_version":parent["version"],
                    "source_parent_event_id":str(command.source_parent_event_id),
                    "decision_method":method,
                    "status":status,
                    "authorizes_execution":False,
                },receipt_id,
            )
            goal_event=None
            if checked:
                goal_event=_publish(
                    cur,actor,command.instance_id,"goal.created.v1",{
                        "goal_id":str(command.proposed_goal_id),
                        "parent_goal_id":str(command.parent_goal_id),
                        "parent_created_event_id":str(parent["created_event_id"]),
                        "parent_version":parent["version"],
                        "source_turn_id":str(parent["source_turn_id"]),
                        "authority":"DELEGATED_PARENT",
                        "user_approved":False,
                        "contribution_kind":"DETERMINISTIC_COUNT_MILESTONE",
                        "method_revision":"mvp03a2.count.v1",
                        "proposal_id":str(command.proposal_id),
                        "depth":parent["depth"]+1,
                    },receipt_id,
                )
                cur.execute(
                    """INSERT INTO goal_records(
                         goal_id,user_id,instance_id,parent_goal_id,depth,
                         title,desired_outcome,success_criterion,horizon,
                         source_conversation_id,source_turn_id,source_quote,
                         created_event_id,creation_authority)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                              'DELEGATED_VERIFIED')""",
                    (command.proposed_goal_id,actor.user_id,command.instance_id,
                     command.parent_goal_id,parent["depth"]+1,
                     command.desired_outcome.strip(),command.desired_outcome.strip(),
                     command.success_criterion.strip(),command.horizon,
                     parent["source_conversation_id"],parent["source_turn_id"],
                     parent["source_quote"],goal_event),
                )
                cur.execute(
                    """UPDATE goal_records SET version=version+1,
                         updated_at=clock_timestamp()
                       WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
                    (command.parent_goal_id,actor.user_id,command.instance_id),
                )
            cur.execute(
                """INSERT INTO goal_subgoal_proposals(
                     proposal_id,proposed_goal_id,user_id,instance_id,
                     parent_goal_id,expected_parent_version,
                     source_parent_event_id,title,desired_outcome,
                     success_criterion,horizon,contribution_note,
                     proposal_digest,status,decision_method,created_event_id,
                     accepted_goal_event_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (command.proposal_id,command.proposed_goal_id,actor.user_id,
                 command.instance_id,command.parent_goal_id,
                 command.expected_parent_version,command.source_parent_event_id,
                 command.title.strip(),command.desired_outcome.strip(),
                 command.success_criterion.strip(),command.horizon,
                 command.contribution_note.strip(),digest,status,method,
                 proposal_event,goal_event),
            )
            result=DerivedSubgoalProposalResult(
                proposal_id=command.proposal_id,
                proposed_goal_id=command.proposed_goal_id,
                parent_goal_id=command.parent_goal_id,status=status,
                decision_method=method,event_id=proposal_event,
                accepted_goal_event_id=goal_event,
            )
            cur.execute(
                """UPDATE command_receipts
                   SET event_id=%s,result_json=%s::jsonb WHERE receipt_id=%s""",
                (proposal_event,result.model_dump_json(),receipt_id),
            )
            return result


def list_pending_subgoal_proposals(
    connection:Connection,actor:TrustedActor,instance_id,*,limit:int=40,
)->tuple[DerivedSubgoalProposalView,...]:
    """Scoped, bounded read-only Goal review queue; not executable work."""
    require_idle(connection)
    if type(actor) is not TrustedActor:
        raise PermissionDenied("subgoal review read requires authenticated user")
    if not 1<=limit<=100:
        raise ValueError("invalid review queue limit")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT proposal_id,proposed_goal_id,parent_goal_id,
                          expected_parent_version,source_parent_event_id,
                          title,desired_outcome,success_criterion,horizon,
                          contribution_note,status,decision_method
                   FROM goal_subgoal_proposals
                   WHERE user_id=%s AND instance_id=%s
                     AND status='PENDING_REVIEW'
                   ORDER BY created_at,proposal_id LIMIT %s""",
                (actor.user_id,instance_id,limit),
            )
            return tuple(
                DerivedSubgoalProposalView.model_validate(dict(row))
                for row in cur.fetchall()
            )
