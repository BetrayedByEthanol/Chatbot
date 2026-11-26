"""Goal-owned recurring progress projection over authoritative Task evidence.

No new worker, goal status mutation, completion counter, or operation grant.
Planning is a read-only proposal; the Task owner rechecks it inside the
existing separate exact approval command and transaction before creating work.
"""
from collections import Counter
from datetime import date,datetime,time,timezone
from psycopg.rows import dict_row

from .conversation import _require_actor
from .goal_state import _goal,_read_selected
from .goal_decomposition import _authorized_lineage
from .operation_contracts import require_idle
from .runtime import VersionConflict
from .task_state import _confirmation_state,_graph_read_lock,MAX_TASKS_PER_GOAL
from .goal_cycle_contracts import (
    recurring_count,cycle_bounds,task_draft,RecurringGoalView,RecurringOccurrenceView,
)


def _instant(day):
    return datetime.combine(day,time.min,tzinfo=timezone.utc)


def _within_goal_window(goal,start,end):
    left,right=_instant(start),_instant(end)
    return (right>goal["created_at"]
            and (goal["evaluation_start_at"] is None or left>=goal["evaluation_start_at"])
            and (goal["evaluation_end_at"] is None or right<=goal["evaluation_end_at"])
            and (goal["target_at"] is None or right<=goal["target_at"]))


def _occurrences(cur,actor,goal,start,end):
    """Read current reports and effective confirmations under Task graph lock."""
    cur.execute(
        """SELECT s.ordinal,t.task_id,t.version AS task_version,t.status AS task_status,
                  t.created_at AS task_created_at,r.report_id,r.report,r.source_turn_id,
                  source.created_at AS source_at, c.confirmation_id,c.report_id AS confirmed_report_id
           FROM goal_recurring_task_slots s
           JOIN goal_tasks t ON t.task_id=s.task_id AND t.goal_id=s.goal_id
             AND t.user_id=s.user_id AND t.instance_id=s.instance_id
           LEFT JOIN LATERAL (
               SELECT r.report_id,r.report,r.source_turn_id,r.source_conversation_id
               FROM task_outcome_reports r JOIN events e ON e.event_id=r.event_id
               WHERE r.task_id=t.task_id AND r.goal_id=t.goal_id
                 AND r.user_id=t.user_id AND r.instance_id=t.instance_id
               ORDER BY e.stream_sequence DESC LIMIT 1
           ) r ON true
           LEFT JOIN conversation_turns source ON source.turn_id=r.source_turn_id
             AND source.conversation_id=r.source_conversation_id
             AND source.user_id=t.user_id AND source.instance_id=t.instance_id
           LEFT JOIN LATERAL (
               SELECT c.confirmation_id,c.report_id FROM task_user_confirmations c
               WHERE c.task_id=t.task_id AND c.goal_id=t.goal_id
                 AND c.user_id=t.user_id AND c.instance_id=t.instance_id
               ORDER BY c.task_version DESC LIMIT 1
           ) c ON true
           WHERE s.goal_id=%s AND s.user_id=%s AND s.instance_id=%s
             AND s.window_start=%s AND s.window_end=%s
           ORDER BY s.ordinal""",
        (goal["goal_id"],actor.user_id,goal["instance_id"],start,end),
    )
    rows=cur.fetchall()
    ids=[row["task_id"] for row in rows]
    _,effective,_,current=_confirmation_state(cur,actor,goal["instance_id"],ids)
    eligible={}
    for row in rows:
        status=row["task_status"]
        if status=="NEEDS_REVIEW":
            state="REQUIRES_REASSESSMENT"
        elif status not in ("USER_REPORTED_DONE","USER_CONFIRMED"):
            state="NONE"
        elif row["report"]!="DONE" or row["source_at"] is None:
            state="REQUIRES_REASSESSMENT"
        elif not (_instant(start)<=row["source_at"]<_instant(end)
                  and row["source_at"]>=row["task_created_at"]):
            state="OUTSIDE_WINDOW"
        elif status=="USER_CONFIRMED":
            state=("USER_CONFIRMED" if effective.get(row["task_id"])
                   and current.get(row["task_id"])==row["confirmation_id"]
                   and row["confirmed_report_id"]==row["report_id"]
                   else "REQUIRES_REASSESSMENT")
        else:
            state="USER_REPORTED"
        eligible[row["task_id"]]=state
    # Reusing a single source across slots cannot inflate a count. Exclude
    # all ambiguous claims, rather than choosing an arbitrary occurrence.
    sources=Counter(row["source_turn_id"] for row in rows
        if eligible[row["task_id"]] in ("USER_REPORTED","USER_CONFIRMED"))
    result=[]
    for row in rows:
        state=eligible[row["task_id"]]
        if state in ("USER_REPORTED","USER_CONFIRMED") and sources[row["source_turn_id"]]>1:
            state="DUPLICATE_SOURCE"
        result.append(RecurringOccurrenceView(
            ordinal=row["ordinal"],task_id=row["task_id"],task_version=row["task_version"],
            task_status=row["task_status"],evidence_state=state,
            report_id=row["report_id"],source_turn_id=row["source_turn_id"],
            confirmation_id=row["confirmation_id"],
        ))
    return tuple(result)


def _plan_locked(cur,actor,goal,window_start=None):
    base=dict(goal_id=goal["goal_id"],goal_version=goal["version"],
        goal_status=goal["status"],desired_outcome=goal["desired_outcome"],
        success_criterion=goal["success_criterion"])
    measure=recurring_count(goal["desired_outcome"],goal["success_criterion"])
    if measure is None:
        return RecurringGoalView(**base,decision="UNSUPPORTED")
    cur.execute("SELECT clock_timestamp() AS now")
    now=cur.fetchone()["now"].astimezone(timezone.utc)
    start,end=cycle_bounds(measure[2],window_start or now.date())
    if window_start is not None and start!=window_start:
        raise ValueError("window_start must be the canonical UTC cycle start")
    base.update(period=measure[2],unit=measure[1],target=measure[3],
                window_start=start,window_end=end)
    # Delegation does not copy dates onto children. Their effective
    # evaluation window is the intersection of every approved ancestor's
    # constraints and their own. Read the authoritative chain rather than
    # denormalizing dates or relying on ACTIVE status alone. Both callers
    # validate lineage; task admission also locks the full ancestry.
    cur.execute(
        """WITH RECURSIVE chain AS (
               SELECT goal_id,parent_goal_id,status,created_at,
                      evaluation_start_at,evaluation_end_at,target_at
               FROM goal_records
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s
               UNION ALL
               SELECT g.goal_id,g.parent_goal_id,g.status,g.created_at,
                      g.evaluation_start_at,g.evaluation_end_at,g.target_at
               FROM goal_records g
               JOIN chain c ON g.goal_id=c.parent_goal_id
               WHERE g.user_id=%s AND g.instance_id=%s
           ) SELECT * FROM chain""",
        (goal["goal_id"],actor.user_id,goal["instance_id"],actor.user_id,goal["instance_id"]),
    )
    chain=cur.fetchall()
    if not chain or any(not _within_goal_window(row,start,end) for row in chain):
        return RecurringGoalView(**base,decision="OUTSIDE_APPROVED_WINDOW")
    rows=_occurrences(cur,actor,goal,start,end)
    confirmed=sum(r.evidence_state=="USER_CONFIRMED" for r in rows)
    reported=sum(r.evidence_state in ("USER_REPORTED","USER_CONFIRMED") for r in rows)
    base.update(occurrences=rows,reported_count=reported,confirmed_count=confirmed)
    # This is a current evidence projection, not a historical as-of read.
    # Corrections can invalidate earlier cycle counts. No roll-up by child.
    if confirmed>=measure[3]:
        return RecurringGoalView(**base,decision="USER_CONFIRMED_TARGET_REPORTED_MET")
    if not start<=now.date()<end:
        return RecurringGoalView(**base,decision="WINDOW_CLOSED")
    if any(row["status"]!="ACTIVE" for row in chain):
        return RecurringGoalView(**base,decision="INACTIVE")
    cur.execute(
        """SELECT EXISTS(SELECT 1 FROM goal_records
             WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s
               AND status<>'ABANDONED')
           OR EXISTS(SELECT 1 FROM goal_subgoal_proposals
             WHERE parent_goal_id=%s AND user_id=%s AND instance_id=%s
               AND status='PENDING_REVIEW') AS competing""",
        (goal["goal_id"],actor.user_id,goal["instance_id"],
         goal["goal_id"],actor.user_id,goal["instance_id"]),
    )
    if cur.fetchone()["competing"]:
        return RecurringGoalView(**base,decision="CHILDREN_OR_REVIEW")
    cur.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER(WHERE s.task_id IS NULL AND t.status<>'CANCELLED') AS unmapped
           FROM goal_tasks t LEFT JOIN goal_recurring_task_slots s
             ON s.task_id=t.task_id AND s.goal_id=t.goal_id
             AND s.user_id=t.user_id AND s.instance_id=t.instance_id
           WHERE t.goal_id=%s AND t.user_id=%s AND t.instance_id=%s""",
        (goal["goal_id"],actor.user_id,goal["instance_id"]),
    )
    counts=cur.fetchone()
    if counts["unmapped"]:
        return RecurringGoalView(**base,decision="UNMAPPED_TASKS")
    # Existing task work, uncertain evidence or cancelled occurrence stops
    # expansion. Do not create a replacement that bypasses user priorities.
    if any(r.evidence_state!="USER_CONFIRMED" for r in rows):
        return RecurringGoalView(**base,decision="WAIT_FOR_EVIDENCE")
    cur.execute(
        """SELECT 1 FROM goal_tasks WHERE user_id=%s AND instance_id=%s
             AND exclusive_only LIMIT 1""",(actor.user_id,goal["instance_id"]),
    )
    if cur.fetchone() is not None:
        return RecurringGoalView(**base,decision="EXCLUSIVE_OTHER")
    ordinal=len(rows)+1
    if counts["total"]>=MAX_TASKS_PER_GOAL or ordinal>measure[3]:
        return RecurringGoalView(**base,decision="BUDGET_EXHAUSTED")
    return RecurringGoalView(**base,decision="PROPOSE_ONE_TASK",
        next_task=task_draft(goal["goal_id"],goal["version"],measure,start,end,ordinal))


def read_recurring_goal(connection,actor,instance_id,goal_id,*,window_start:date|None=None):
    """Read one scoped goal/cycle; never persist a task or count."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            _graph_read_lock(cur,actor,instance_id)
            goal=_goal(cur,actor,instance_id,goal_id,lock="SHARE")
            _authorized_lineage(cur,actor,instance_id,goal)
            return _plan_locked(cur,actor,goal,window_start)


def validate_recurring_task(cur,actor,goal,command):
    """Task owner calls under graph/ancestry locks, after exact user approval."""
    _authorized_lineage(cur,actor,command.instance_id,goal)
    plan=_plan_locked(cur,actor,goal,command.window_start)
    if plan.decision!="PROPOSE_ONE_TASK" or plan.next_task is None:
        raise VersionConflict("recurring decomposition stopped: "+plan.decision)
    fields=set(type(plan.next_task).model_fields)
    if command.model_dump(include=fields)!=plan.next_task.model_dump():
        raise VersionConflict("recurring task does not match the current owner-derived proposal")
