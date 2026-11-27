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
    proven_count_milestone,verified_count_measure,proposal_digest,
)
from .goal_state import (
    _MAX_CHILDREN,_MAX_SUBGOAL_DEPTH,_locked_active_goal_ancestry,_has_existing_goal_tasks,
    _read_selected,
    _publish,_selected,
)
from .lifecycle import TrustedPlatformActor
from .contracts import TrustedActor
from .operation_contracts import (
    OperationLease,require_claim,require_current_authority,require_idle,
)
from .goal_autonomy_contracts import (
    GOAL_EVALUATION_OPERATION,GOAL_EVALUATION_SOURCE,
)
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
                       "DELEGATED_MILESTONE","DELEGATED_VERIFIED",
                       "DELEGATED_REVIEWED")
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
    command:ProposeDerivedSubgoalCommand,*,lease:OperationLease|None=None,
    model_id:str|None=None,evidence_ids:tuple[UUID,...]|None=None,
)->DerivedSubgoalProposalResult:
    """Stage, or auto-accept a *proved* quantitative child, in one transaction.

    This entry point is for trusted Goal evaluation only. P2 Background
    admission and operation authorization are intentionally not exposed here.
    """
    require_idle(connection)
    background=(type(actor) is TrustedPlatformActor
                and actor.source_id==GOAL_EVALUATION_SOURCE)
    if (type(actor) is not TrustedPlatformActor
        or actor.source_id not in ("goal.decomposition.v1",
                                  GOAL_EVALUATION_SOURCE)
        or (background and (lease is None or not isinstance(model_id,str)
                            or not 1<=len(model_id)<=128))
        or (not background and (lease is not None or model_id is not None))):
        raise PermissionDenied("subgoal proposal requires trusted Goal evaluator")
    if (background and evidence_ids is None) or (evidence_ids is not None and (
        not background or len(evidence_ids)>3 or len(set(evidence_ids))!=len(evidence_ids)
    )):
        raise PermissionDenied("invalid Goal evaluation evidence pins")
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

            if background:
                # P0 lease and selection are fenced in THIS Goal transaction,
                # not merely at the worker's earlier inference snapshot.
                # Lock order: command receipt -> operation -> selection ->
                # sorted ancestor goals. Background cannot reuse Foreground's
                # unrestricted trusted-evaluator source.
                op=require_claim(cur,lease)
                if (op["operation_type"]!=GOAL_EVALUATION_OPERATION
                    or op["origin"]!="GOAL"
                    or op["priority_class"]!="P2"
                    or op["effect_class"]!="NONE"
                    or op["execution_policy"]!="BACKGROUND_RESUMABLE"
                    or op["user_id"]!=actor.user_id
                    or op["instance_id"]!=command.instance_id
                    or op["selection_generation"]!=command.expected_generation):
                    raise PermissionDenied("Goal evaluation lease does not authorize this proposal")
                require_current_authority(cur,op)
                cur.execute(
                    """SELECT 1 FROM goal_autonomy_jobs job
                       JOIN background_ready_epochs ready
                         ON ready.user_id=job.user_id
                        AND ready.instance_id=job.instance_id
                        AND ready.background_epoch=job.background_epoch
                       JOIN background_preparation_epochs epoch
                         ON epoch.user_id=job.user_id
                        AND epoch.instance_id=job.instance_id
                        AND epoch.background_epoch=job.background_epoch
                       WHERE job.operation_id=%s AND job.user_id=%s
                         AND job.instance_id=%s AND job.goal_id=%s
                         AND job.parent_version=%s AND job.status='STAGED'
                         AND job.expected_generation=%s
                         AND job.background_epoch=%s
                         AND epoch.selection_generation=%s
                         AND epoch.status='PENDING'
                         AND epoch.preparation_stage='READY'""",
                    (lease.operation_id,actor.user_id,command.instance_id,
                     command.parent_goal_id,command.expected_parent_version,
                     command.expected_generation,lease.background_epoch,
                     command.expected_generation),
                )
                if cur.fetchone() is None:
                    raise PermissionDenied("Goal job lacks current READY authorization")
            else:
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
            if _has_existing_goal_tasks(cur,actor,command.instance_id,command.parent_goal_id):
                raise VersionConflict("goal decomposition stopped at existing tasks")

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
            accepted_measure=(
                verified_count_measure(
                    command.desired_outcome,command.success_criterion,
                ) if checked else None
            )
            if checked:
                # The parent is locked FOR UPDATE through this transaction,
                # serializing sibling creation with all other Goal writes.
                # Compare the same canonical measure used by admission,
                # not the raw wording ("1 book" vs "one books", etc.).
                # The existing child budget makes this scan bounded.
                cur.execute(
                    """SELECT desired_outcome,success_criterion
                       FROM goal_records WHERE parent_goal_id=%s
                         AND user_id=%s AND instance_id=%s""",
                    (command.parent_goal_id,actor.user_id,command.instance_id),
                )
                for sibling in cur.fetchall():
                    if verified_count_measure(
                        sibling["desired_outcome"],sibling["success_criterion"],
                    )==accepted_measure:
                        raise VersionConflict("equivalent subgoal already exists")
            else:
                # Prevent unbounded model candidate accumulation.
                cur.execute(
                    """SELECT count(*) AS n FROM goal_subgoal_proposals
                       WHERE parent_goal_id=%s AND user_id=%s
                         AND instance_id=%s AND status='PENDING_REVIEW'""",
                    (command.parent_goal_id,actor.user_id,command.instance_id),
                )
                if cur.fetchone()["n"]>=24:
                    raise VersionConflict("subgoal review candidate budget reached")

            digest=proposal_digest(
                command,accepted_measure=accepted_measure,
            )
            cur.execute(
                """SELECT 1 FROM goal_subgoal_proposals
                   WHERE user_id=%s AND instance_id=%s AND parent_goal_id=%s
                     AND proposal_digest=%s""",
                (actor.user_id,command.instance_id,command.parent_goal_id,digest),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("duplicate subgoal proposal")

            if background:
                # Revalidate after acquiring all potentially blocking Goal
                # ancestry/sibling locks, before writing any proposal event.
                # The final operation UPDATE repeats this live-clock fence.
                require_claim(cur,lease)
                require_current_authority(cur,op)

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
                     accepted_goal_event_id,input_evidence_ids)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (command.proposal_id,command.proposed_goal_id,actor.user_id,
                 command.instance_id,command.parent_goal_id,
                 command.expected_parent_version,command.source_parent_event_id,
                 command.title.strip(),command.desired_outcome.strip(),
                 command.success_criterion.strip(),command.horizon,
                 command.contribution_note.strip(),digest,status,method,
                 proposal_event,goal_event,list(evidence_ids) if evidence_ids is not None else None),
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
            if background:
                # No independent commit between Goal mutation, job completion
                # and P0 completion: a crash cannot strand a changed parent
                # version behind an unfinalized Goal evaluation operation.
                from .goal_autonomy import _finalize_goal_evaluation_locked
                _finalize_goal_evaluation_locked(
                    cur,lease,status="PROPOSED",model_id=model_id,
                    proposal_id=command.proposal_id,
                    proposed_goal_id=command.proposed_goal_id,
                    evidence_ids=evidence_ids,evidence_goal_id=command.parent_goal_id,
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
