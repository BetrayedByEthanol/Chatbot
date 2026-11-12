"""P3-02 goal-owned task graph: authorization, CAS, DAG and read-only readiness.

No tool dispatch or agent-side verification. A task is never completed because
the agent narrated a step or the user reported doing it.
"""
import json
from datetime import timedelta
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
    AddTaskDependencyCommand, CancelTaskCommand, CreateTaskCommand,
    ReportTaskOutcomeCommand,
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


def _task(cur, actor: TrustedActor, instance_id: UUID, task_id: UUID) -> dict:
    cur.execute(
        """SELECT * FROM goal_tasks WHERE task_id=%s
           AND user_id=%s AND instance_id=%s FOR UPDATE""",
        (task_id, actor.user_id, instance_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PermissionDenied("task outside user/persona scope")
    return row


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
        if goal["status"]!="ACTIVE":
            raise VersionConflict("cannot add tasks to paused/abandoned goal")
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
                 completion_criterion,evidence_requirement,executor,capability,
                 source_conversation_id,source_turn_id,approval_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.task_id,command.goal_id,actor.user_id,command.instance_id,
             command.title.strip(),command.completion_criterion.strip(),
             command.evidence_requirement.strip(),command.executor,command.capability,
             command.conversation_id,command.evidence_turn_id,
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
    def apply(cur,receipt):
        # The containing goal row serializes competing edge inserts but not
        # unrelated goal graphs. Both task FK scopes are independently checked.
        task=_task(cur,actor,command.instance_id,command.task_id)
        goal=_goal(cur,actor,command.instance_id,task["goal_id"])
        if goal["version"]!=command.expected_goal_version:
            raise VersionConflict("goal graph version changed")
        if goal["status"]!="ACTIVE" or task["status"]!="PLANNED":
            raise VersionConflict("task/goal cannot accept dependencies")
        cur.execute(
            """SELECT goal_id,status FROM goal_tasks
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (command.prerequisite_task_id,actor.user_id,command.instance_id),
        )
        other=cur.fetchone()
        if other is None or other["goal_id"]!=task["goal_id"] or other["status"]=="CANCELLED":
            raise PermissionDenied("dependency outside active scoped goal")
        cur.execute(
            """SELECT count(*) AS n FROM task_dependencies WHERE task_id=%s""",
            (command.task_id,),
        )
        if cur.fetchone()["n"]>=MAX_DEPENDENCIES_PER_TASK:
            raise VersionConflict("dependency fan-in limit")
        cur.execute(
            """SELECT 1 FROM task_dependencies
               WHERE task_id=%s AND prerequisite_task_id=%s""",
            (command.task_id,command.prerequisite_task_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("duplicate dependency")
        # A->B means A waits for B; reject if B can already reach A.
        cur.execute(
            """WITH RECURSIVE reachable(id) AS (
                 SELECT %s::uuid
                 UNION
                 SELECT d.prerequisite_task_id FROM task_dependencies d
                 JOIN reachable r ON d.task_id=r.id
               )
               SELECT 1 FROM reachable WHERE id=%s LIMIT 1""",
            (command.prerequisite_task_id,command.task_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("dependency cycle")
        event=_publish(cur,actor,command.instance_id,"goal.task.dependency.added.v1",{
            "task_id":str(command.task_id),
            "prerequisite_task_id":str(command.prerequisite_task_id),
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
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"]=="CANCELLED":
            raise VersionConflict("task already cancelled")
        event=_publish(cur,actor,command.instance_id,"goal.task.cancelled.v1",{
            "task_id":str(command.task_id),
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """UPDATE goal_tasks SET status='CANCELLED',
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
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"]=="CANCELLED":
            raise VersionConflict("cancelled task cannot accept progress")
        # Reporting DONE is evidence, not VERIFIED task completion.
        status="USER_REPORTED_DONE" if command.report=="DONE" else "PLANNED"
        event=_publish(cur,actor,command.instance_id,"goal.task.outcome.reported.v1",{
            "task_id":str(command.task_id),"report":command.report,
            "epistemic_status":"USER_REPORTED","classification_status":"UNREVIEWED",
        },receipt)
        cur.execute(
            """INSERT INTO task_outcome_reports(
                 report_id,task_id,goal_id,user_id,instance_id,report,
                 source_conversation_id,source_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),task["task_id"],task["goal_id"],actor.user_id,
             command.instance_id,command.report,command.conversation_id,
             command.evidence_turn_id,event),
        )
        cur.execute(
            """UPDATE goal_tasks SET status=%s,version=version+1,
                 updated_at=clock_timestamp() WHERE task_id=%s RETURNING version""",
            (status,command.task_id),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=cur.fetchone()["version"],
                                 event_id=event,action="OUTCOME_REPORTED")
    return _command(connection,actor,command,"goal.task.outcome.report.v1",apply)


def set_task_priority(connection: Connection, actor: TrustedActor,
                      command: SetTaskPriorityCommand) -> TaskCommandResult:
    def apply(cur,receipt):
        task=_task(cur,actor,command.instance_id,command.task_id)
        if task["version"]!=command.expected_task_version:
            raise VersionConflict("task changed")
        if task["status"]=="CANCELLED":
            raise VersionConflict("cancelled task cannot be prioritized")
        if command.other_task_id is not None:
            cur.execute(
                """SELECT goal_id FROM goal_tasks WHERE task_id=%s
                   AND user_id=%s AND instance_id=%s""",
                (command.other_task_id,actor.user_id,command.instance_id),
            )
            other=cur.fetchone()
            if other is None or other["goal_id"]!=task["goal_id"]:
                raise PermissionDenied("relative priority must target task in same goal")
        event=_publish(cur,actor,command.instance_id,"goal.task.priority.set.v1",{
            "task_id":str(command.task_id),"rule":command.rule,
            "other_task_id":str(command.other_task_id) if command.other_task_id else None,
            "deadline":command.deadline.isoformat() if command.deadline else None,
        },receipt)
        cur.execute(
            """INSERT INTO task_priority_rules(
                 priority_id,task_id,goal_id,user_id,instance_id,rule,
                 other_task_id,deadline,task_version,
                 source_conversation_id,source_turn_id,approval_turn_id,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),task["task_id"],task["goal_id"],actor.user_id,
             command.instance_id,command.rule,command.other_task_id,
             command.deadline,task["version"]+1,
             command.conversation_id,command.evidence_turn_id,
             command.approval_turn_id,event),
        )
        cur.execute(
            """UPDATE goal_tasks SET version=version+1,
               updated_at=clock_timestamp() WHERE task_id=%s RETURNING version""",
            (command.task_id,),
        )
        return TaskCommandResult(task_id=command.task_id,
                                 version=cur.fetchone()["version"],
                                 event_id=event,action="PRIORITY_SET")
    return _command(connection,actor,command,"goal.task.priority.set.v1",apply)


def list_tasks(connection: Connection, actor: TrustedActor, instance_id: UUID,
               goal_id: UUID, *, limit: int=64) -> tuple[TaskView,...]:
    """Derive readiness; never label a user report as VERIFIED.

    Reads are short and fenced with the selected-persona authority lock.
    Goal ancestors (including paused/abandoned parents) gate readiness.
    Agent tasks stay blocked until the capability/permission gateway is wired.
    """
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
                   )
                   SELECT status FROM ancestors""",
                (goal_id,actor.user_id,instance_id,actor.user_id,instance_id),
            )
            active=all(x["status"]=="ACTIVE" for x in cur.fetchall())
            cur.execute(
                """SELECT * FROM goal_tasks WHERE goal_id=%s
                   AND user_id=%s AND instance_id=%s
                   ORDER BY created_at,task_id LIMIT %s""",
                (goal_id,actor.user_id,instance_id,limit),
            )
            rows=cur.fetchall()
            ids=[x["task_id"] for x in rows]
            if not ids:
                return ()
            cur.execute(
                """SELECT task_id,prerequisite_task_id FROM task_dependencies
                   WHERE task_id=ANY(%s)""",(ids,),
            )
            edges={}
            for x in cur.fetchall():
                edges.setdefault(x["task_id"],[]).append(x["prerequisite_task_id"])
            cur.execute(
                """SELECT task_id,rule,other_task_id,deadline
                   FROM task_priority_rules WHERE task_id=ANY(%s)
                   ORDER BY created_at,priority_id""",(ids,),
            )
            priorities={}
            for x in cur.fetchall():
                priorities.setdefault(x["task_id"],[]).append(x)
            # P3-02 has no verification authority: prerequisites cannot be
            # satisfied by USER_REPORTED_DONE. All existing predecessors block.
            result=[]
            for task in rows:
                blockers=[]
                if not active:
                    blockers.append("GOAL_OR_ANCESTOR_INACTIVE")
                if task["status"]=="USER_REPORTED_DONE":
                    blockers.append("AWAITING_VERIFICATION")
                if task["status"]=="CANCELLED":
                    blockers.append("CANCELLED")
                dep=tuple(sorted(edges.get(task["task_id"],()),key=str))
                if dep:
                    blockers.append("PREREQUISITE_NOT_VERIFIED")
                if task["executor"]=="AGENT":
                    blockers.append("CAPABILITY_AND_PERMISSION_UNCHECKED")
                rules=priorities.get(task["task_id"],[])
                suspension=[x["rule"] for x in rules if x["rule"] in ("SUSPEND","RESUME")]
                if suspension and suspension[-1]=="SUSPEND":
                    blockers.append("USER_SUSPENDED")
                preferred=tuple(x["other_task_id"] for x in rules
                                if x["rule"]=="PREFER_OVER")
                deadlines=[x["deadline"] if x["rule"]=="SET_DEADLINE" else None
                           for x in rules if x["rule"] in ("SET_DEADLINE","CLEAR_DEADLINE")]
                result.append(TaskView(
                    task_id=task["task_id"],goal_id=goal_id,title=task["title"],
                    executor=task["executor"],completion_criterion=task["completion_criterion"],
                    evidence_requirement=task["evidence_requirement"],
                    capability=task["capability"],status=task["status"],
                    version=task["version"],actionable=not blockers,
                    blockers=tuple(blockers),prerequisite_ids=dep,
                    preferred_over=preferred,
                    run_after=tuple(x["other_task_id"] for x in rules
                                    if x["rule"]=="RUN_AFTER"),
                    deadline=deadlines[-1] if deadlines else None,
                ))
            return tuple(result)
