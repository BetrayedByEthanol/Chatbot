"""P3-02 goal-owned task state, evidence, dependency DAG and priority rules.

All writes use P0 receipts, selection fencing, short PostgreSQL transactions,
and event/outbox. No operation is enqueued and no autonomous work is admitted.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable
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
    CreateTaskCommand, SetTaskPriorityCommand, TaskCommandResult,
    TaskUserCommand, TaskView, task_approval_phrase,
)

_MAX_TASKS_PER_GOAL = 64
_MAX_DEPENDENCIES = 32


def _approval(cur, actor: TrustedActor, cmd: TaskUserCommand, source: dict) -> None:
    """Bind confirmation to one proposed action, not merely a quoted intention."""
    if cmd.approval_turn_id is None or cmd.approval_turn_id == cmd.evidence_turn_id:
        raise PermissionDenied("task action requires a separate user confirmation")
    cur.execute(
        """SELECT input_text,created_at FROM conversation_turns
           WHERE conversation_id=%s AND turn_id=%s
             AND user_id=%s AND instance_id=%s""",
        (cmd.conversation_id, cmd.approval_turn_id, actor.user_id, cmd.instance_id),
    )
    confirmation = cur.fetchone()
    if (confirmation is None or
        confirmation["input_text"].strip() != task_approval_phrase(cmd) or
        confirmation["created_at"] < source["created_at"] or
        confirmation["created_at"] > source["created_at"] + timedelta(days=1)):
        raise PermissionDenied("task confirmation does not match the proposed action")


def _command(
    connection: Connection, actor: TrustedActor, cmd: TaskUserCommand,
    command_type: str, apply: Callable,
) -> TaskCommandResult:
    require_idle(connection)
    _require_actor(actor)
    receipt_id = uuid4()
    fingerprint = _fingerprint(cmd)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id, cmd.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("task command outside persona scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,cmd.instance_id,
                 command_type,cmd.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (actor.user_id,cmd.instance_id,command_type,cmd.idempotency_key),
                )
                prior = cur.fetchone()
                if (prior is None or prior["request_fingerprint"] != fingerprint
                    or prior["result_json"] is None):
                    raise IdempotencyConflict("task idempotency key reused differently")
                return TaskCommandResult.model_validate(prior["result_json"]).model_copy(
                    update={"replayed": True}
                )
            _selected(cur, actor, cmd)
            source = _source(cur, actor, cmd)
            _approval(cur, actor, cmd, source)
            result = apply(cur, actor, cmd, receipt_id)
            cur.execute(
                """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
                   WHERE receipt_id=%s""",
                (result.event_id,json.dumps(result.model_dump(mode="json")),receipt_id),
            )
            return result


def _graph_lock(cur, actor: TrustedActor, instance_id: UUID) -> None:
    """Serialize only DAG/priority/completion edits for one User × Persona."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"p3-task-dag:{actor.user_id}:{instance_id}",),
    )


def _task(cur, actor: TrustedActor, instance_id: UUID, task_id: UUID,
          *, lock: str = "UPDATE") -> dict:
    if lock not in ("UPDATE", "SHARE"):
        raise ValueError("invalid task lock")
    cur.execute(
        f"""SELECT * FROM task_records WHERE task_id=%s
            AND user_id=%s AND instance_id=%s FOR {lock}""",
        (task_id,actor.user_id,instance_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PermissionDenied("task outside selected user/persona scope")
    return row


def _require_version(row: dict, version: int) -> None:
    if row["version"] != version:
        raise VersionConflict("task version changed")


def _goal_chain_active(cur, actor: TrustedActor, instance_id: UUID,
                       goal_id: UUID) -> bool:
    """Do not treat a child of a paused/abandoned ancestor as actionable."""
    cur.execute(
        """WITH RECURSIVE chain AS (
             SELECT goal_id,parent_goal_id,status FROM goal_records
              WHERE goal_id=%s AND user_id=%s AND instance_id=%s
             UNION ALL
             SELECT g.goal_id,g.parent_goal_id,g.status
               FROM goal_records g JOIN chain c ON c.parent_goal_id=g.goal_id
              WHERE g.user_id=%s AND g.instance_id=%s
           )
           SELECT count(*) AS nodes,
                  count(*) FILTER (WHERE status='ACTIVE') AS active
             FROM chain""",
        (goal_id,actor.user_id,instance_id,actor.user_id,instance_id),
    )
    result = cur.fetchone()
    return result["nodes"] > 0 and result["nodes"] == result["active"]


def _would_cycle(cur, actor: TrustedActor, instance_id: UUID,
                 task_id: UUID, new_predecessor: UUID) -> bool:
    """Check graph with ordinary dependencies AND RUN_AFTER ordering edges."""
    cur.execute(
        """WITH RECURSIVE reachable(id) AS (
             SELECT %s::uuid
             UNION
             SELECT edge.predecessor
               FROM reachable r
               CROSS JOIN LATERAL (
                 SELECT depends_on_id AS predecessor FROM task_dependencies
                   WHERE task_id=r.id AND user_id=%s AND instance_id=%s
                 UNION
                 SELECT run_after_task_id FROM task_records
                   WHERE task_id=r.id AND user_id=%s AND instance_id=%s
                     AND run_after_task_id IS NOT NULL
               ) edge
           )
           SELECT EXISTS(SELECT 1 FROM reachable WHERE id=%s) AS cycle""",
        (new_predecessor,actor.user_id,instance_id,
         actor.user_id,instance_id,task_id),
    )
    return cur.fetchone()["cycle"]


def create_task(
    connection: Connection, actor: TrustedActor, cmd: CreateTaskCommand,
) -> TaskCommandResult:
    """Create bounded explicitly authorized work; never dispatch an operation."""
    def apply(cur, who, c, receipt):
        # Per-goal serialization also fences the bounded task count.
        goal = _goal(cur, who, c.instance_id, c.goal_id)
        if goal["version"] != c.expected_goal_version:
            raise VersionConflict("goal version changed during task planning")
        if not _goal_chain_active(cur, who, c.instance_id, c.goal_id):
            raise VersionConflict("task belongs to inactive goal or ancestor")
        cur.execute(
            """SELECT count(*) AS n FROM task_records
               WHERE user_id=%s AND instance_id=%s AND goal_id=%s""",
            (who.user_id,c.instance_id,c.goal_id),
        )
        if cur.fetchone()["n"] >= _MAX_TASKS_PER_GOAL:
            raise VersionConflict("goal task count exceeds bounded decomposition")
        cur.execute("SELECT 1 FROM task_records WHERE task_id=%s", (c.task_id,))
        if cur.fetchone() is not None:
            raise VersionConflict("task identifier already used")
        policy = "USER_CONFIRMATION" if c.executor == "USER" else "OPERATION_RESULT"
        event_id = _publish(cur,who,c.instance_id,"task.created.v1",{
            "task_id":str(c.task_id),"goal_id":str(c.goal_id),
            "executor":c.executor,"policy":policy,
            "approval_turn_id":str(c.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO task_records(
                 task_id,user_id,instance_id,goal_id,title,action_description,
                 completion_criterion,executor,evidence_policy,capability,
                 source_conversation_id,source_turn_id,approval_turn_id,
                 source_quote,created_event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (c.task_id,who.user_id,c.instance_id,c.goal_id,c.title.strip(),
             c.action_description.strip(),c.completion_criterion.strip(),
             c.executor,policy,c.capability,c.conversation_id,
             c.evidence_turn_id,c.approval_turn_id,c.evidence_quote,event_id),
        )
        return TaskCommandResult(
            task_id=c.task_id,task_version=1,event_id=event_id,action="TASK_CREATED"
        )
    return _command(connection,actor,cmd,"task.create.v1",apply)


def add_task_dependency(
    connection: Connection, actor: TrustedActor, cmd: AddTaskDependencyCommand,
) -> TaskCommandResult:
    """Reject cross-scope edges, duplicate edges, cycles and stale revisions."""
    def apply(cur, who, c, receipt):
        _graph_lock(cur,who,c.instance_id)
        task = _task(cur,who,c.instance_id,c.task_id)
        _require_version(task,c.expected_task_version)
        if task["status"] != "OPEN":
            raise VersionConflict("cannot modify completed/cancelled task dependency")
        _task(cur,who,c.instance_id,c.depends_on_id,lock="SHARE")
        if _would_cycle(cur,who,c.instance_id,c.task_id,c.depends_on_id):
            raise VersionConflict("task dependency would introduce a cycle")
        cur.execute(
            """SELECT 1 FROM task_dependencies
               WHERE task_id=%s AND depends_on_id=%s""",
            (c.task_id,c.depends_on_id),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("task dependency already exists")
        cur.execute(
            "SELECT count(*) AS n FROM task_dependencies WHERE task_id=%s",
            (c.task_id,),
        )
        if cur.fetchone()["n"] >= _MAX_DEPENDENCIES:
            raise VersionConflict("task dependency limit exceeded")
        version = task["version"]+1
        event_id = _publish(cur,who,c.instance_id,"task.dependency.added.v1",{
            "task_id":str(c.task_id),"depends_on_id":str(c.depends_on_id),
            "version":version,"approval_turn_id":str(c.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO task_dependencies(
                 task_id,depends_on_id,user_id,instance_id,created_event_id
               ) VALUES (%s,%s,%s,%s,%s)""",
            (c.task_id,c.depends_on_id,who.user_id,c.instance_id,event_id),
        )
        cur.execute(
            """UPDATE task_records SET version=%s,updated_at=clock_timestamp()
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (version,c.task_id,who.user_id,c.instance_id),
        )
        return TaskCommandResult(
            task_id=c.task_id,task_version=version,event_id=event_id,
            action="DEPENDENCY_ADDED",
        )
    return _command(connection,actor,cmd,"task.dependency.add.v1",apply)


def _state_snapshot(cur, actor: TrustedActor, instance_id: UUID):
    cur.execute(
        """SELECT goal_id,parent_goal_id,status FROM goal_records
           WHERE user_id=%s AND instance_id=%s""",
        (actor.user_id,instance_id),
    )
    goals = {g["goal_id"]:g for g in cur.fetchall()}
    cur.execute(
        """SELECT task_id,status,exclusive_only FROM task_records
           WHERE user_id=%s AND instance_id=%s""",
        (actor.user_id,instance_id),
    )
    tasks = {t["task_id"]:t for t in cur.fetchall()}
    cur.execute(
        """SELECT task_id,depends_on_id FROM task_dependencies
           WHERE user_id=%s AND instance_id=%s""",
        (actor.user_id,instance_id),
    )
    dependencies = defaultdict(set)
    for edge in cur.fetchall():
        dependencies[edge["task_id"]].add(edge["depends_on_id"])
    exclusive = {tid for tid,t in tasks.items() if t["exclusive_only"] and t["status"] == "OPEN"}
    return goals,tasks,dependencies,exclusive


def _readiness(row, goals, tasks, deps, exclusive, now):
    if row["status"] == "CANCELLED":
        return "CANCELLED", ()
    if row["status"] == "USER_CONFIRMED":
        return "COMPLETE_USER_CONFIRMED", ()
    ancestor = row["goal_id"]
    while ancestor is not None:
        goal = goals.get(ancestor)
        if goal is None or goal["status"] != "ACTIVE":
            return "GOAL_INACTIVE", ()
        ancestor = goal["parent_goal_id"]
    if row["suspended"]:
        return "SUSPENDED", ()
    if exclusive and row["task_id"] not in exclusive:
        return "EXCLUSIVE_OTHER", ()
    all_deps = set(deps.get(row["task_id"], ()))
    if row["run_after_task_id"] is not None:
        all_deps.add(row["run_after_task_id"])
    unmet = tuple(sorted(
        (dep for dep in all_deps
         if dep not in tasks or tasks[dep]["status"] != "USER_CONFIRMED"),
        key=str,
    ))
    if unmet:
        return "WAITING_DEPENDENCIES", unmet
    if row["deadline"] is not None and row["deadline"] <= now:
        return "DEADLINE_EXPIRED", ()
    if row["executor"] == "AGENT" and row["capability"] is None:
        return "NEEDS_CAPABILITY", ()
    return "READY", ()


def confirm_user_task(
    connection: Connection, actor: TrustedActor, cmd: ConfirmUserTaskCommand,
) -> TaskCommandResult:
    """User-confirmed task outcome; not a tool-verified or goal-level success."""
    def apply(cur,who,c,receipt):
        _graph_lock(cur,who,c.instance_id)
        row = _task(cur,who,c.instance_id,c.task_id)
        _require_version(row,c.expected_task_version)
        if row["executor"] != "USER" or row["evidence_policy"] != "USER_CONFIRMATION":
            raise PermissionDenied("agent work cannot be completed by user claim")
        goals,tasks,deps,exclusive = _state_snapshot(cur,who,c.instance_id)
        actionability,_ = _readiness(
            row,goals,tasks,deps,exclusive,datetime.now(timezone.utc),
        )
        if actionability not in ("READY", "DEADLINE_EXPIRED"):
            raise VersionConflict("task not actionable for user confirmation: "+actionability)
        new_version = row["version"]+1
        event_id = _publish(cur,who,c.instance_id,"task.user_completion.reported.v1",{
            "task_id":str(c.task_id),"epistemic_status":"USER_CONFIRMED",
            "source_turn_id":str(c.evidence_turn_id),
            "approval_turn_id":str(c.approval_turn_id),
            "task_version":new_version,
        },receipt)
        cur.execute(
            """INSERT INTO task_completion_reports(
                 report_id,task_id,user_id,instance_id,task_version,
                 source_conversation_id,source_turn_id,approval_turn_id,
                 source_quote,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (c.report_id,c.task_id,who.user_id,c.instance_id,new_version,
             c.conversation_id,c.evidence_turn_id,c.approval_turn_id,
             c.evidence_quote,event_id),
        )
        cur.execute(
            """UPDATE task_records SET status='USER_CONFIRMED',
                 exclusive_only=false,version=%s,updated_at=clock_timestamp()
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (new_version,c.task_id,who.user_id,c.instance_id),
        )
        return TaskCommandResult(
            task_id=c.task_id,task_version=new_version,event_id=event_id,
            action="USER_COMPLETION_RECORDED",
        )
    return _command(connection,actor,cmd,"task.user.confirm.v1",apply)


def cancel_task(
    connection: Connection, actor: TrustedActor, cmd: CancelTaskCommand,
) -> TaskCommandResult:
    """Explicit user cancel; does not silently cancel dependent tasks."""
    def apply(cur,who,c,receipt):
        _graph_lock(cur,who,c.instance_id)
        row = _task(cur,who,c.instance_id,c.task_id)
        _require_version(row,c.expected_task_version)
        if row["status"] != "OPEN":
            raise VersionConflict("only open tasks can be cancelled")
        version = row["version"]+1
        event_id = _publish(cur,who,c.instance_id,"task.cancelled.v1",{
            "task_id":str(c.task_id),"version":version,
            "approval_turn_id":str(c.approval_turn_id),
        },receipt)
        cur.execute(
            """UPDATE task_records SET status='CANCELLED',exclusive_only=false,
                 version=%s,updated_at=clock_timestamp() WHERE task_id=%s
                   AND user_id=%s AND instance_id=%s""",
            (version,c.task_id,who.user_id,c.instance_id),
        )
        return TaskCommandResult(
            task_id=c.task_id,task_version=version,event_id=event_id,
            action="TASK_CANCELLED",
        )
    return _command(connection,actor,cmd,"task.cancel.v1",apply)


def set_task_priority(
    connection: Connection, actor: TrustedActor, cmd: SetTaskPriorityCommand,
) -> TaskCommandResult:
    """Persist user constraints distinctly: PREFER, ONLY, SUSPEND, RUN_AFTER, DEADLINE."""
    def apply(cur,who,c,receipt):
        _graph_lock(cur,who,c.instance_id)
        task = _task(cur,who,c.instance_id,c.task_id)
        _require_version(task,c.expected_task_version)
        if task["status"] != "OPEN":
            raise VersionConflict("cannot reprioritize completed/cancelled task")
        changes = {}
        if c.rule in ("PREFER_OVER","RUN_AFTER"):
            _task(cur,who,c.instance_id,c.related_task_id,lock="SHARE")
            if c.rule == "RUN_AFTER":
                if _would_cycle(cur,who,c.instance_id,c.task_id,c.related_task_id):
                    raise VersionConflict("RUN_AFTER would introduce dependency cycle")
                changes["run_after_task_id"] = c.related_task_id
            else:
                changes["preferred_over_task_id"] = c.related_task_id
        elif c.rule == "CLEAR_PREFERENCE":
            changes["preferred_over_task_id"] = None
        elif c.rule == "CLEAR_RUN_AFTER":
            changes["run_after_task_id"] = None
        elif c.rule in ("SUSPEND","RESUME"):
            changes["suspended"] = c.rule=="SUSPEND"
        elif c.rule in ("ONLY","CLEAR_ONLY"):
            changes["exclusive_only"] = c.rule=="ONLY"
            if c.rule=="ONLY":
                cur.execute(
                    """SELECT 1 FROM task_records WHERE user_id=%s AND instance_id=%s
                         AND exclusive_only AND task_id<>%s""",
                    (who.user_id,c.instance_id,c.task_id),
                )
                if cur.fetchone() is not None:
                    raise VersionConflict("another task has exclusive user priority")
        elif c.rule == "SET_DEADLINE":
            if c.deadline <= datetime.now(timezone.utc):
                raise VersionConflict("cannot set a past task deadline")
            changes["deadline"] = c.deadline
        elif c.rule == "CLEAR_DEADLINE":
            changes["deadline"] = None
        else:
            raise ValueError("unknown task priority rule")
        if all(task[k]==v for k,v in changes.items()):
            raise VersionConflict("priority rule has no state change")
        version = task["version"]+1
        event_id = _publish(cur,who,c.instance_id,"task.priority.changed.v1",{
            "task_id":str(c.task_id),"version":version,"rule":c.rule,
            "related_task_id":str(c.related_task_id) if c.related_task_id else None,
            "deadline":c.deadline.isoformat() if c.deadline else None,
            "approval_turn_id":str(c.approval_turn_id),
        },receipt)
        cols = ",".join(f"{key}=%s" for key in changes)
        cur.execute(
            f"""UPDATE task_records SET {cols},version=%s,
                 updated_at=clock_timestamp()
               WHERE task_id=%s AND user_id=%s AND instance_id=%s""",
            (*changes.values(),version,c.task_id,who.user_id,c.instance_id),
        )
        cur.execute(
            """INSERT INTO task_priority_revisions(
                 revision_id,task_id,user_id,instance_id,task_version,rule_kind,
                 related_task_id,deadline,source_conversation_id,source_turn_id,
                 approval_turn_id,source_quote,event_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(),c.task_id,who.user_id,c.instance_id,version,c.rule,
             c.related_task_id,c.deadline,c.conversation_id,c.evidence_turn_id,
             c.approval_turn_id,c.evidence_quote,event_id),
        )
        return TaskCommandResult(
            task_id=c.task_id,task_version=version,event_id=event_id,
            action="PRIORITY_CHANGED",
        )
    return _command(connection,actor,cmd,"task.priority.set.v1",apply)


def list_tasks(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    *, goal_id: UUID | None = None, limit: int = 120,
) -> tuple[TaskView, ...]:
    """Selected-persona read with current derived eligibility; never queues work."""
    require_idle(connection)
    _require_actor(actor)
    if not 1 <= limit <= 300:
        raise ValueError("task list bound must be 1..300")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            if goal_id is not None:
                _goal(cur,actor,instance_id,goal_id,lock="SHARE")
            cur.execute(
                """SELECT * FROM task_records WHERE user_id=%s AND instance_id=%s
                     AND (%s::uuid IS NULL OR goal_id=%s)
                   ORDER BY created_at,task_id LIMIT %s""",
                (actor.user_id,instance_id,goal_id,goal_id,limit),
            )
            rows = cur.fetchall()
            goals,tasks,deps,exclusive = _state_snapshot(cur,actor,instance_id)
            now = datetime.now(timezone.utc)
            return tuple(TaskView(
                task_id=row["task_id"],goal_id=row["goal_id"],title=row["title"],
                action_description=row["action_description"],
                completion_criterion=row["completion_criterion"],
                executor=row["executor"],evidence_policy=row["evidence_policy"],
                capability=row["capability"],status=row["status"],version=row["version"],
                actionability=_readiness(
                    row,goals,tasks,deps,exclusive,now
                )[0],
                unmet_dependencies=_readiness(
                    row,goals,tasks,deps,exclusive,now
                )[1],
                suspended=row["suspended"],exclusive_only=row["exclusive_only"],
                preferred_over_task_id=row["preferred_over_task_id"],
                run_after_task_id=row["run_after_task_id"],deadline=row["deadline"],
            ) for row in rows)
