"""P3-02 goal-owned task graph: authorization, CAS, DAG and read-only readiness.

No tool dispatch or agent-side verification. A task is never completed because
the agent narrated a step or the user reported doing it.
"""
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .goal_state import _goal, _publish, _read_selected, _selected, _source
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
    _fingerprint,
)
from .task_contracts import (
    AddTaskDependencyCommand, CancelTaskCommand, ConfirmUserTaskCommand,
    CorrectTaskOutcomeCommand, CreateTaskCommand, ReportTaskOutcomeCommand,
    SetTaskPriorityCommand, TaskCommandResult, TaskUserCommand, TaskView,
    task_approval_phrase,
)

MAX_TASKS_PER_GOAL = 64
MAX_DEPENDENCIES_PER_TASK = 16


def _approval(cur, actor: TrustedActor, command: TaskUserCommand, source: dict) -> None:
    """Require a separate exact action-specific user confirmation."""
    if command.approval_turn_id is None or command.approval_turn_id == command.evidence_turn_id:
        raise PermissionDenied("task change requires separate user approval")
    cur.execute(
        """SELECT input_text,created_at FROM conversation_turns
           WHERE user_id=%s AND instance_id=%s AND conversation_id=%s
             AND turn_id=%s""",
        (actor.user_id, command.instance_id,
         command.conversation_id, command.approval_turn_id),
    )
    row = cur.fetchone()
    if (row is None or row["input_text"].strip() != task_approval_phrase(command)
        or row["created_at"] < source["created_at"]
        or row["created_at"] > source["created_at"] + timedelta(days=1)):
        raise PermissionDenied("task approval is not bound to the proposed action")


def _graph_lock(cur, actor: TrustedActor, instance_id: UUID) -> None:
    """Serialize graph/priority and user-confirmation edits within one persona.

    No lock is held across model/tool work. Independent goal creation and
    non-graph state updates retain the existing domain-owned concurrency.
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"p3-task-dag:{actor.user_id}:{instance_id}",))


def _task(cur, actor: TrustedActor, instance_id: UUID, task_id: UUID,
          *, lock: str = "UPDATE") -> dict:
    if lock not in ("UPDATE", "SHARE"):
        raise ValueError("invalid task row lock")
    cur.execute(
        f"""SELECT * FROM goal_tasks WHERE task_id=%s
           AND user_id=%s AND instance_id=%s FOR {lock}""",
        (task_id, actor.user_id, instance_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PermissionDenied("task outside user/persona scope")
    return row



def _active_goal_chain(cur, actor: TrustedActor, instance_id: UUID,
                       goal_id: UUID) -> None:
    """Lock the full immutable parent chain to fence concurrent pauses.

    P3-01 status edits use FOR UPDATE on a goal row. FOR SHARE on every
    ancestor holds its ACTIVE status through this short task transaction.
    """
    cur.execute(
        """WITH RECURSIVE chain AS (
             SELECT goal_id,parent_goal_id FROM goal_records
             WHERE goal_id=%s AND user_id=%s AND instance_id=%s
             UNION ALL
             SELECT g.goal_id,g.parent_goal_id FROM goal_records g
             JOIN chain c ON g.goal_id=c.parent_goal_id
             WHERE g.user_id=%s AND g.instance_id=%s
           ) SELECT goal_id FROM chain""",
        (goal_id,actor.user_id,instance_id,actor.user_id,instance_id),
    )
    ids=tuple(row["goal_id"] for row in cur.fetchall())
    if not ids:
        raise PermissionDenied("goal outside user/persona scope")
    cur.execute(
        """SELECT goal_id,status FROM goal_records
           WHERE goal_id=ANY(%s) AND user_id=%s AND instance_id=%s
           ORDER BY goal_id FOR SHARE""",
        (list(ids),actor.user_id,instance_id),
    )
    chain=cur.fetchall()
    if len(chain)!=len(ids) or any(row["status"]!="ACTIVE" for row in chain):
        raise VersionConflict("goal or ancestor paused or abandoned")


def _would_cycle(cur, actor: TrustedActor, instance_id: UUID,
                 task_id: UUID, predecessor: UUID) -> bool:
    """Test reachability across prerequisites and current RUN_AFTER edges."""
    cur.execute(
        """WITH RECURSIVE reachable(id) AS (
             SELECT %s::uuid
             UNION
             SELECT edge.prereq FROM reachable r
             CROSS JOIN LATERAL (
               SELECT d.prerequisite_task_id AS prereq
                 FROM task_dependencies d
                 WHERE d.task_id=r.id AND d.user_id=%s AND d.instance_id=%s
               UNION
               SELECT t.run_after_task_id
                 FROM goal_tasks t
                 WHERE t.task_id=r.id AND t.user_id=%s AND t.instance_id=%s
                   AND t.run_after_task_id IS NOT NULL
             ) edge
           ) SELECT EXISTS(SELECT 1 FROM reachable WHERE id=%s) AS cycle""",
        (predecessor,actor.user_id,instance_id,actor.user_id,instance_id,task_id),
    )
    return cur.fetchone()["cycle"]


def _latest_report(cur, actor: TrustedActor, instance_id: UUID,
                   task_id: UUID) -> dict | None:
    """Order report evidence by durable event sequence, not timestamp.

    Task row locking and the persona graph lock serialize mutations.
    The goal journal's stream_sequence is unique and increasing for reports.
    """
    cur.execute(
        """SELECT r.report_id,r.report FROM task_outcome_reports r
           JOIN events e ON e.event_id=r.event_id
           WHERE r.task_id=%s AND r.user_id=%s AND r.instance_id=%s
           ORDER BY e.stream_sequence DESC LIMIT 1""",
        (task_id,actor.user_id,instance_id),
    )
    return cur.fetchone()


def _unmet_prerequisites(cur, actor: TrustedActor, instance_id: UUID,
                         task_ids: list[UUID]) -> dict[UUID, tuple[UUID, ...]]:
    """Compute transitive effective confirmation over dependency + RUN_AFTER DAG.

    A historical USER_CONFIRMED task is not a valid prerequisite if any of
    its ancestors has since been corrected, cancelled or left unconfirmed.
    The read projection preserves each task's historical status; it never
    silently revokes downstream user testimony.
    """
    if not task_ids:
        return {}
    cur.execute(
        """WITH RECURSIVE closure(root_id,node_id) AS (
             SELECT task_id,task_id FROM goal_tasks
             WHERE task_id=ANY(%s) AND user_id=%s AND instance_id=%s
             UNION
             SELECT c.root_id,edge.predecessor_id
             FROM closure c JOIN (
                  SELECT task_id,prerequisite_task_id AS predecessor_id,
                         user_id,instance_id
                  FROM task_dependencies
                  UNION ALL
                  SELECT task_id,run_after_task_id AS predecessor_id,
                         user_id,instance_id
                  FROM goal_tasks WHERE run_after_task_id IS NOT NULL
             ) edge ON edge.task_id=c.node_id
             WHERE edge.user_id=%s AND edge.instance_id=%s
           )
           SELECT c.root_id,c.node_id FROM closure c
           JOIN goal_tasks predecessor
             ON predecessor.task_id=c.node_id
            AND predecessor.user_id=%s AND predecessor.instance_id=%s
           WHERE c.root_id<>c.node_id
             AND predecessor.status<>'USER_CONFIRMED'""",
        (task_ids,actor.user_id,instance_id,
         actor.user_id,instance_id,actor.user_id,instance_id),
    )
    result: dict[UUID,set[UUID]]={}
    for row in cur.fetchall():
        result.setdefault(row["root_id"],set()).add(row["node_id"])
    return {root:tuple(sorted(ids,key=str)) for root,ids in result.items()}


def _command(connection: Connection, actor: TrustedActor,
             command: TaskUserCommand, command_type: str, apply) -> TaskCommandResult:
    require_idle(connection)
    _require_actor(actor)
    receipt_id = uuid4()
    fingerprint = _fingerprint(command)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s""",
                (actor.user_id, command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("task command outside persona scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,command_type,
                 command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,command_type,command.idempotency_key),
                )
                prior=cur.fetchone()
                if prior is None or prior["request_fingerprint"]!=fingerprint or prior["result_json"] is None:
                    raise IdempotencyConflict("task command idempotency key conflict")
                return TaskCommandResult.model_validate(prior["result_json"]).model_copy(
                    update={"replayed":True},
                )
            _selected(cur,actor,command)
            source=_source(cur,actor,command)
            _approval(cur,actor,command,source)
            result=apply(cur,receipt_id)
            cur.execute(
                """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
                   WHERE receipt_id=%s""",
                (result.event_id,json.dumps(result.model_dump(mode="json")),receipt_id),
            )
            return result


def create_task(connection: Connection, actor: TrustedActor,
                command: CreateTaskCommand) -> TaskCommandResult:
    def apply(cur, receipt):
        goal=_goal(cur,actor,command.instance_id,command.goal_id)
        if goal["version"]!=command.expected_goal_version:
            raise VersionConflict("goal changed during task creation")
        _active_goal_chain(cur,actor,command.instance_id,command.goal_id)
        cur.execute(
            """SELECT count(*) AS n FROM goal_tasks
               WHERE user_id=%s AND instance_id=%s AND goal_id=%s""",
            (actor.user_id,command.instance_id,command.goal_id),
        )
        if cur.fetchone()["n"]>=MAX_TASKS_PER_GOAL:
            raise VersionConflict("task count budget exhausted")
        event=_publish(cur,actor,command.instance_id,"goal.task.created.v1",{
            "task_id":str(command.task_id),"goal_id":str(command.goal_id),
            "executor":command.executor,
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO goal_tasks(
                 task_id,goal_id,user_id,instance_id,title,
                 action_description,completion_criterion,evidence_requirement,
                 executor,evidence_policy,capability,
                 source_conversation_id,source_turn_id,approval_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.task_id,command.goal_id,actor.user_id,command.instance_id,
             command.title.strip(),command.action_description.strip(),
             command.completion_criterion.strip(),
             command.evidence_requirement.strip(),command.executor,
             "USER_CONFIRMATION" if command.executor=="USER" else "OPERATION_RESULT",
             command.capability,command.conversation_id,command.evidence_turn_id,
             command.approval_turn_id,event),
        )
        cur.execute(
            """UPDATE goal_records SET version=version+1,updated_at=clock_timestamp()
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
            (command.goal_id,actor.user_id,command.instance_id),
        )
        return TaskCommandResult(task_id=command.task_id,version=1,
                                 event_id=event,action="TASK_CREATED")
    return _command(connection,actor,command,"goal.task.create.v1",apply)


def add_task_dependency(connection: Connection, actor: TrustedActor,
                        command: AddTaskDependencyCommand) -> TaskCommandResult:
    """Persist a scoped prerequisite; reject cross-persona and mixed-edge cycles."""
    def apply(cur,receipt):
        # A single per-persona graph transaction lock prevents write-skew
        # across different target goals and RUN_AFTER priority changes.
        _graph_lock(cur,actor,command.instance_id)
        task=_task(cur,actor,command.instance_id,command.task_id)
        goal=_goal(cur,actor,command.instance_id,task["goal_id"])
        if goal["version"]!=command.expected_goal_version:
            raise VersionConflict("goal graph version changed")
        if task["status"]!="PLANNED":
            raise VersionConflict("cannot add dependency to reported or completed work")
        _active_goal_chain(cur,actor,command.instance_id,task["goal_id"])
        other=_task(cur,actor,command.instance_id,command.prerequisite_task_id,lock="SHARE")
        if other["status"]=="CANCELLED":
            raise VersionConflict("cancelled task cannot serve as a new prerequisite")
        _active_goal_chain(cur,actor,command.instance_id,other["goal_id"])
        cur.execute("SELECT count(*) AS n FROM task_dependencies WHERE task_id=%s",
                    (command.task_id,))
        if cur.fetchone()["n"]>=MAX_DEPENDENCIES_PER_TASK:
            raise VersionConflict("dependency fan-in limit")
        cur.execute(
            """SELECT 1 FROM task_dependencies
               WHERE task_id=%s AND prerequisite_task_id=%s""",
            (command.task_id,command.prerequisite_task_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("duplicate dependency")
        if _would_cycle(cur,actor,command.instance_id,
                        command.task_id,command.prerequisite_task_id):
            raise VersionConflict("dependency cycle")
        event=_publish(cur,actor,command.instance_id,"goal.task.dependency.added.v1",{
            "task_id":str(command.task_id),
            "prerequisite_task_id":str(command.prerequisite_task_id),
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO task_dependencies(
                 task_id,prerequisite_task_id,goal_id,user_id,instance_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s)""",
            (command.task_id,command.prerequisite_task_id,task["goal_id"],
             actor.user_id,command.instance_id,event),
        )
        cur.execute(
            """UPDATE goal_records SET version=version+1,updated_at=clock_timestamp()
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s""",
            (task["goal_id"],actor.user_id,command.instance_id),
        )
        cur.execute(
            """UPDATE goal_tasks SET version=version+1,updated_at=clock_timestamp()
               WHERE task_id=%s RETURNING version""",(command.task_id,),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=cur.fetchone()["version"],
                                 event_id=event,action="DEPENDENCY_ADDED")
    return _command(connection,actor,command,"goal.task.dependency.add.v1",apply)


def cancel_task(connection: Connection, actor: TrustedActor,
                command: CancelTaskCommand) -> TaskCommandResult:
    """Explicitly cancel planned/reported work without altering its approved goal.

    This does not cancel external operations; task-owned execution does not
    exist in P3-02. Later operation admission must read task cancellation.
    """
    def apply(cur,receipt):
        _graph_lock(cur,actor,command.instance_id)
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"] in ("CANCELLED","USER_CONFIRMED"):
            raise VersionConflict("confirmed or cancelled task cannot be cancelled")
        event=_publish(cur,actor,command.instance_id,"goal.task.cancelled.v1",{
            "task_id":str(command.task_id),
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """UPDATE goal_tasks SET status='CANCELLED',exclusive_only=false,
                 version=version+1,updated_at=clock_timestamp()
               WHERE task_id=%s RETURNING version""",
            (command.task_id,),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=cur.fetchone()["version"],
                                 event_id=event,action="TASK_CANCELLED")
    return _command(connection,actor,command,"goal.task.cancel.v1",apply)


def report_task_outcome(connection: Connection, actor: TrustedActor,
                        command: ReportTaskOutcomeCommand) -> TaskCommandResult:
    def apply(cur,receipt):
        # All evidence-driven readiness changes serialize with confirmations,
        # graph updates and corrections within this selected persona.
        _graph_lock(cur,actor,command.instance_id)
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"] in ("CANCELLED","USER_CONFIRMED"):
            raise VersionConflict("terminal task cannot accept progress")
        # Reporting DONE is evidence, not VERIFIED task completion.
        status="USER_REPORTED_DONE" if command.report=="DONE" else "PLANNED"
        report_id=uuid4()
        event=_publish(cur,actor,command.instance_id,"goal.task.outcome.reported.v1",{
            "task_id":str(command.task_id),"report":command.report,
            "report_id":str(report_id),
            "epistemic_status":"USER_REPORTED","classification_status":"UNREVIEWED",
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO task_outcome_reports(
                 report_id,task_id,goal_id,user_id,instance_id,report,
                 source_conversation_id,source_turn_id,source_quote,
                 approval_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (report_id,task["task_id"],task["goal_id"],actor.user_id,
             command.instance_id,command.report,command.conversation_id,
             command.evidence_turn_id,command.evidence_quote,
             command.approval_turn_id,event),
        )
        cur.execute(
            """UPDATE goal_tasks SET status=%s,version=version+1,
                 updated_at=clock_timestamp() WHERE task_id=%s RETURNING version""",
            (status,command.task_id),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=cur.fetchone()["version"],
                                 event_id=event,action="OUTCOME_REPORTED",
                                 report_id=report_id)
    return _command(connection,actor,command,"goal.task.outcome.report.v1",apply)


def confirm_user_task(connection: Connection, actor: TrustedActor,
                      command: ConfirmUserTaskCommand) -> TaskCommandResult:
    """Convert a reported action into explicit USER_CONFIRMED, never VERIFIED.

    A separate action-bound user confirmation permits dependent USER work to
    proceed. AGENT tasks need a future operation-result verification gate.
    Goal/ancestor statuses are SHARE-locked through commit (Codex #19 race).
    """
    def apply(cur,receipt):
        _graph_lock(cur,actor,command.instance_id)
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["executor"]!="USER" or task["evidence_policy"]!="USER_CONFIRMATION":
            raise PermissionDenied("only user-executed work accepts user confirmation")
        if task["status"]!="USER_REPORTED_DONE":
            raise VersionConflict("user completion needs an outstanding DONE report")
        cur.execute(
            """SELECT report_id FROM task_outcome_reports
               WHERE report_id=%s AND task_id=%s AND goal_id=%s
                 AND user_id=%s AND instance_id=%s AND report='DONE'""",
            (command.report_id,command.task_id,task["goal_id"],
             actor.user_id,command.instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("task report does not belong to this action")
        latest=_latest_report(cur,actor,command.instance_id,command.task_id)
        if (latest is None or latest["report_id"]!=command.report_id
            or latest["report"]!="DONE"):
            raise VersionConflict("DONE report was superseded by newer task evidence")
        _active_goal_chain(cur,actor,command.instance_id,task["goal_id"])
        if task["suspended"]:
            raise VersionConflict("user-suspended task cannot be confirmed actionable")
        cur.execute(
            """SELECT 1 FROM goal_tasks WHERE user_id=%s AND instance_id=%s
                 AND exclusive_only AND task_id<>%s LIMIT 1""",
            (actor.user_id,command.instance_id,command.task_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("another task has exclusive priority")
        # A prerequisite can be historically USER_CONFIRMED but no longer
        # effective after a correction further up the DAG. Check the complete
        # dependency closure, not only immediate predecessor status.
        if _unmet_prerequisites(
            cur,actor,command.instance_id,[command.task_id],
        ).get(command.task_id):
            raise VersionConflict("prerequisite confirmation is no longer effective")
        version=task["version"]+1
        confirmation_id=uuid4()
        event=_publish(cur,actor,command.instance_id,"goal.task.user_confirmed.v1",{
            "task_id":str(command.task_id),"report_id":str(command.report_id),
            "confirmation_id":str(confirmation_id),
            "epistemic_status":"USER_CONFIRMED","independently_verified":False,
            "approval_turn_id":str(command.approval_turn_id),
            "task_version":version,
        },receipt)
        cur.execute(
            """INSERT INTO task_user_confirmations(
                 confirmation_id,report_id,task_id,goal_id,user_id,instance_id,
                 task_version,source_conversation_id,source_turn_id,
                 approval_turn_id,source_quote,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (confirmation_id,command.report_id,command.task_id,task["goal_id"],
             actor.user_id,command.instance_id,version,command.conversation_id,
             command.evidence_turn_id,command.approval_turn_id,
             command.evidence_quote,event),
        )
        cur.execute(
            """UPDATE goal_tasks SET status='USER_CONFIRMED',exclusive_only=false,
                 version=%s,updated_at=clock_timestamp()
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (version,command.task_id,actor.user_id,command.instance_id),
        )
        return TaskCommandResult(task_id=command.task_id,version=version,
                                 event_id=event,action="USER_COMPLETION_CONFIRMED",
                                 report_id=command.report_id,
                                 confirmation_id=confirmation_id)
    return _command(connection,actor,command,"goal.task.user.confirm.v1",apply)


def set_task_priority(connection: Connection, actor: TrustedActor,
                      command: SetTaskPriorityCommand) -> TaskCommandResult:
    """Version, enforce and record separate durable user priority constraints."""
    def apply(cur,receipt):
        _graph_lock(cur,actor,command.instance_id)
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"] in ("CANCELLED","USER_CONFIRMED"):
            raise VersionConflict("terminal task cannot be prioritized")
        changes={}
        if command.rule in ("PREFER_OVER","RUN_AFTER"):
            other=_task(cur,actor,command.instance_id,command.other_task_id,lock="SHARE")
            if other["status"]=="CANCELLED":
                raise VersionConflict("cancelled task is not a valid priority target")
            if command.rule=="RUN_AFTER":
                if _would_cycle(cur,actor,command.instance_id,
                                command.task_id,command.other_task_id):
                    raise VersionConflict("RUN_AFTER would introduce a cycle")
                changes["run_after_task_id"]=command.other_task_id
            else:
                changes["preferred_over_task_id"]=command.other_task_id
        elif command.rule=="CLEAR_PREFERENCE":
            changes["preferred_over_task_id"]=None
        elif command.rule=="CLEAR_RUN_AFTER":
            changes["run_after_task_id"]=None
        elif command.rule in ("SUSPEND","RESUME"):
            changes["suspended"]=command.rule=="SUSPEND"
        elif command.rule in ("ONLY","CLEAR_ONLY"):
            changes["exclusive_only"]=command.rule=="ONLY"
            if command.rule=="ONLY":
                cur.execute(
                    """SELECT 1 FROM goal_tasks
                       WHERE user_id=%s AND instance_id=%s
                         AND exclusive_only AND task_id<>%s LIMIT 1""",
                    (actor.user_id,command.instance_id,command.task_id),
                )
                if cur.fetchone() is not None:
                    raise VersionConflict("another task already has exclusive priority")
        elif command.rule=="SET_DEADLINE":
            if command.deadline<=datetime.now(timezone.utc):
                raise VersionConflict("task deadline must be in the future")
            changes["deadline"]=command.deadline
        elif command.rule=="CLEAR_DEADLINE":
            changes["deadline"]=None
        else:
            raise ValueError("unsupported task priority rule")
        if all(task[key]==value for key,value in changes.items()):
            raise VersionConflict("priority would not change task state")
        event=_publish(cur,actor,command.instance_id,"goal.task.priority.set.v1",{
            "task_id":str(command.task_id),"rule":command.rule,
            "other_task_id":str(command.other_task_id) if command.other_task_id else None,
            "deadline":command.deadline.isoformat() if command.deadline else None,
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        version=task["version"]+1
        setters=",".join(f"{key}=%s" for key in changes)
        cur.execute(
            f"""UPDATE goal_tasks SET {setters},version=%s,
                 updated_at=clock_timestamp()
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (*changes.values(),version,command.task_id,
             actor.user_id,command.instance_id),
        )
        cur.execute(
            """INSERT INTO task_priority_rules(
                 priority_id,task_id,goal_id,user_id,instance_id,rule,
                 other_task_id,deadline,task_version,
                 source_conversation_id,source_turn_id,source_quote,
                 approval_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),task["task_id"],task["goal_id"],actor.user_id,
             command.instance_id,command.rule,command.other_task_id,
             command.deadline,version,command.conversation_id,
             command.evidence_turn_id,command.evidence_quote,
             command.approval_turn_id,event),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=version,event_id=event,action="PRIORITY_SET")
    return _command(connection,actor,command,"goal.task.priority.set.v1",apply)


def list_tasks(connection: Connection, actor: TrustedActor, instance_id: UUID,
               goal_id: UUID, *, limit: int=64) -> tuple[TaskView,...]:
    """Derive scoped planning readiness, never imply task operation authority."""
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=MAX_TASKS_PER_GOAL:
        raise ValueError("invalid task list limit")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            _goal(cur,actor,instance_id,goal_id,lock="SHARE")
            cur.execute(
                """WITH RECURSIVE ancestors AS (
                     SELECT goal_id,parent_goal_id,status FROM goal_records
                     WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                     UNION ALL
                     SELECT g.goal_id,g.parent_goal_id,g.status FROM goal_records g
                     JOIN ancestors a ON g.goal_id=a.parent_goal_id
                     WHERE g.user_id=%s AND g.instance_id=%s
                   ) SELECT status FROM ancestors""",
                (goal_id,actor.user_id,instance_id,actor.user_id,instance_id),
            )
            active=all(row["status"]=="ACTIVE" for row in cur.fetchall())
            cur.execute(
                """SELECT * FROM goal_tasks WHERE goal_id=%s
                   AND user_id=%s AND instance_id=%s
                   ORDER BY created_at,task_id LIMIT %s""",
                (goal_id,actor.user_id,instance_id,limit),
            )
            rows=cur.fetchall()
            ids=[row["task_id"] for row in rows]
            if not ids:
                return ()
            cur.execute(
                """SELECT task_id,prerequisite_task_id FROM task_dependencies
                   WHERE task_id=ANY(%s) AND user_id=%s AND instance_id=%s""",
                (ids,actor.user_id,instance_id),
            )
            edges={}
            for row in cur.fetchall():
                edges.setdefault(row["task_id"],set()).add(row["prerequisite_task_id"])
            predecessors=set()
            for task in rows:
                predecessors.update(edges.get(task["task_id"],()))
                if task["run_after_task_id"] is not None:
                    predecessors.add(task["run_after_task_id"])
            statuses={}
            if predecessors:
                cur.execute(
                    """SELECT task_id,status FROM goal_tasks
                       WHERE task_id=ANY(%s) AND user_id=%s AND instance_id=%s""",
                    (list(predecessors),actor.user_id,instance_id),
                )
                statuses={row["task_id"]:row["status"] for row in cur.fetchall()}
            cur.execute(
                """SELECT task_id FROM goal_tasks
                   WHERE user_id=%s AND instance_id=%s AND exclusive_only""",
                (actor.user_id,instance_id),
            )
            only=cur.fetchone()
            only_id=only["task_id"] if only else None
            now=datetime.now(timezone.utc)
            result=[]
            for task in rows:
                blockers=[]
                if task["status"]=="CANCELLED":
                    blockers.append("CANCELLED")
                elif task["status"]=="USER_CONFIRMED":
                    blockers.append("USER_CONFIRMED")
                elif task["status"]=="USER_REPORTED_DONE":
                    blockers.append("AWAITING_USER_CONFIRMATION")
                if not active:
                    blockers.append("GOAL_OR_ANCESTOR_INACTIVE")
                if task["suspended"]:
                    blockers.append("USER_SUSPENDED")
                if only_id is not None and only_id!=task["task_id"]:
                    blockers.append("EXCLUSIVE_OTHER")
                prerequisites=set(edges.get(task["task_id"],()))
                if task["run_after_task_id"] is not None:
                    prerequisites.add(task["run_after_task_id"])
                unmet=tuple(sorted(
                    (p for p in prerequisites if statuses.get(p)!="USER_CONFIRMED"),
                    key=str,
                ))
                if unmet:
                    blockers.append("PREREQUISITE_NOT_CONFIRMED")
                if task["deadline"] is not None and task["deadline"]<=now:
                    blockers.append("DEADLINE_EXPIRED")
                if task["executor"]=="AGENT":
                    blockers.append("CAPABILITY_AND_PERMISSION_UNCHECKED")
                result.append(TaskView(
                    task_id=task["task_id"],goal_id=goal_id,title=task["title"],
                    action_description=task["action_description"],
                    executor=task["executor"],evidence_policy=task["evidence_policy"],
                    completion_criterion=task["completion_criterion"],
                    evidence_requirement=task["evidence_requirement"],
                    capability=task["capability"],status=task["status"],
                    version=task["version"],actionable=not blockers,
                    blockers=tuple(blockers),
                    prerequisite_ids=tuple(sorted(edges.get(task["task_id"],()),key=str)),
                    preferred_over=(task["preferred_over_task_id"],)
                        if task["preferred_over_task_id"] else (),
                    run_after=(task["run_after_task_id"],)
                        if task["run_after_task_id"] else (),
                    deadline=task["deadline"],
                    exclusive_only=task["exclusive_only"],
                    unmet_dependencies=unmet,
                ))
            return tuple(result)
