"""Read-only ALIGN-01 validators working through existing domain-owned records.

These checks do NOT modify goal, task or procedure state. A domain-specific
approval is still required for corrections, new strategy selections or method
changes; P4-04 owns actual autonomous step admission.
"""
from datetime import datetime, timezone
from uuid import UUID

from .contracts import TrustedActor
from .task_state import _confirmation_state, _graph_read_lock
from .runtime import VersionConflict


def _outcome(decision,reason,task,goal_versions=None,run=None,priority=1):
    return dict(
        decision=decision,reason=reason,task_version=task["version"],
        goal_id=task["goal_id"],goal_versions=goal_versions or {},
        procedure_run_id=run["run_id"] if run else None,
        procedure_run_version=run["version"] if run else None,
        priority_rank=priority,
    )


def validate_task_alignment(cur,user_id:UUID,instance_id:UUID,task:dict)->dict:
    """A task-specific, deterministic owner's projection; no LLM or tools.

    Version pins and explicit approved run/strategy bindings are checked.
    Semantic parent contributions with no persisted approval are REVIEW,
    not inferred from attractive prose. Unrelated tasks remain independently
    eligible to pass the later P4-04 authority gate.
    """
    priority=0 if task["exclusive_only"] else 1
    cur.execute(
        """WITH RECURSIVE chain AS (
             SELECT goal_id,parent_goal_id,status,version,created_event_id,
                    source_turn_id,0 AS distance
             FROM goal_records
             WHERE user_id=%s AND instance_id=%s AND goal_id=%s
             UNION ALL
             SELECT g.goal_id,g.parent_goal_id,g.status,g.version,
                    g.created_event_id,g.source_turn_id,c.distance+1
             FROM chain c JOIN goal_records g ON g.goal_id=c.parent_goal_id
             WHERE g.user_id=%s AND g.instance_id=%s AND c.distance < 6
           )
           SELECT c.*,e.trust_level AS source_trust,
                  e.payload->>'source_turn_id' AS event_source
           FROM chain c LEFT JOIN events e ON e.event_id=c.created_event_id
           ORDER BY c.distance""",
        (user_id,instance_id,task["goal_id"],user_id,instance_id),
    )
    goals=cur.fetchall()
    versions={str(g["goal_id"]):g["version"] for g in goals}
    if not goals or goals[0]["goal_id"]!=task["goal_id"] or len(goals)>6:
        return _outcome("BLOCKED","GOAL_LINEAGE_INVALID",task,versions,priority=priority)
    if (goals[-1]["parent_goal_id"] is not None
        or len(set(g["goal_id"] for g in goals))!=len(goals)
        or any(g["source_trust"]!="AUTHENTICATED_USER"
               or g["event_source"]!=str(g["source_turn_id"]) for g in goals)):
        return _outcome("BLOCKED","GOAL_LINEAGE_INVALID",task,versions,priority=priority)
    if any(g["status"]!="ACTIVE" for g in goals):
        return _outcome("BLOCKED","GOAL_INACTIVE",task,versions,priority=priority)
    if task["executor"]!="AGENT":
        return _outcome("BLOCKED","NOT_AGENT_WORK",task,versions,priority=priority)
    # An agent Task must originate in a real authenticated Task-owned event.
    # Cognitive proposals, even source-linked ones, are not action approval.
    cur.execute(
        """SELECT trust_level,payload->>'task_id' AS claimed_task
           FROM events WHERE event_id=%s AND user_id=%s AND instance_id=%s""",
        (task["event_id"],user_id,instance_id),
    )
    created=cur.fetchone()
    if (created is None or created["trust_level"]!="AUTHENTICATED_USER"
        or created["claimed_task"]!=str(task["task_id"])):
        return _outcome("BLOCKED","SOURCE_UNVERIFIED",task,versions,priority=priority)
    if task["status"]!="PLANNED":
        return _outcome("BLOCKED","TASK_INACTIVE",task,versions,priority=priority)
    if task["suspended"]:
        return _outcome("BLOCKED","PRIORITY_SUSPENDED",task,versions,priority=priority)
    if task["deadline"] is not None and task["deadline"]<=datetime.now(timezone.utc):
        return _outcome("BLOCKED","DEADLINE_EXPIRED",task,versions,priority=priority)
    cur.execute(
        """SELECT 1 FROM goal_tasks
           WHERE user_id=%s AND instance_id=%s AND exclusive_only
             AND task_id<>%s LIMIT 1""",
        (user_id,instance_id,task["task_id"]),
    )
    if cur.fetchone():
        return _outcome("BLOCKED","EXCLUSIVE_OTHER",task,versions,priority=priority)
    unmet,_,_,_=_confirmation_state(
        cur,TrustedActor(user_id=user_id,source_id="background.alignment"),
        instance_id,[task["task_id"]],
    )
    if unmet.get(task["task_id"]):
        return _outcome("BLOCKED","PREREQUISITE_UNMET",task,versions,priority=priority)
    # An inferred contribution between a child goal and its parent is not
    # yet represented by an explicitly approved contribution contract.
    if len(goals)>1:
        return _outcome("REVIEW","GOAL_LINEAGE_UNVERIFIED",task,versions,priority=priority)

    cur.execute(
        """SELECT * FROM procedure_runs
           WHERE user_id=%s AND instance_id=%s AND task_id=%s
           ORDER BY created_at DESC,run_id DESC LIMIT 1""",
        (user_id,instance_id,task["task_id"]),
    )
    run=cur.fetchone()
    if run is None:
        return _outcome("REVIEW","MISSING_APPROVED_RUN",task,versions,priority=priority)
    if run["status"]!="ACTIVE":
        return _outcome("BLOCKED","RUN_INACTIVE",task,versions,run,priority)
    cur.execute(
        """SELECT i.persona_id FROM persona_instances i
           WHERE i.user_id=%s AND i.id=%s""",(user_id,instance_id),
    )
    persona=cur.fetchone()
    cur.execute(
        """SELECT 1 FROM procedure_template_versions tv
           JOIN procedure_templates t ON t.template_id=tv.template_id
             AND t.persona_id=tv.persona_id
           WHERE tv.template_id=%s AND tv.version=%s
             AND tv.persona_id=%s AND t.approved""",
        (run["template_id"],run["template_version"],
         persona["persona_id"] if persona else None),
    )
    if (persona is None or run["persona_id"]!=persona["persona_id"]
        or cur.fetchone() is None):
        return _outcome("BLOCKED","PROCEDURE_SCOPE_INVALID",task,versions,run,priority)
    cur.execute(
        """SELECT e.trust_level,e.payload FROM events e
           WHERE e.event_id=%s AND e.user_id=%s AND e.instance_id=%s""",
        (run["created_event_id"],user_id,instance_id),
    )
    proof=cur.fetchone()
    if proof is None or proof["trust_level"]!="AUTHENTICATED_USER":
        return _outcome("BLOCKED","SOURCE_UNVERIFIED",task,versions,run,priority)
    # A Goal's explicitly selected strategy is not interchangeable with
    # an unrelated generic procedure. If no personalized variant ties the
    # run to the new strategy, it needs a renewed domain-owned decision.
    cur.execute(
        """SELECT strategy_id FROM goal_strategy_selections
           WHERE goal_id=%s AND user_id=%s AND instance_id=%s
           ORDER BY goal_version DESC LIMIT 1""",
        (task["goal_id"],user_id,instance_id),
    )
    chosen=cur.fetchone()
    if chosen is not None and run["variant_id"] is None:
        return _outcome("REVIEW","STRATEGY_CHANGED",task,versions,run,priority)
    if run["variant_id"] is not None:
        cur.execute(
            """SELECT v.goal_id,v.strategy_id,v.current_revision
               FROM personalized_procedures v
               WHERE v.variant_id=%s AND v.user_id=%s AND v.instance_id=%s
                 AND v.template_id=%s""",
            (run["variant_id"],user_id,instance_id,run["template_id"]),
        )
        variant=cur.fetchone()
        if variant is None or variant["current_revision"]!=run["variant_revision"]:
            return _outcome("REVIEW","PROCEDURE_REVISION_CHANGED",task,versions,run,priority)
        # Generic variants cannot silently stand in for a newly selected
        # Goal strategy. Only a Goal/Strategy-bound owner-approved variant
        # can discharge that relationship.
        if chosen is not None and variant["goal_id"] is None:
            return _outcome("REVIEW","STRATEGY_CHANGED",task,versions,run,priority)
        if variant["goal_id"] is not None:
            if variant["goal_id"]!=task["goal_id"]:
                return _outcome("BLOCKED","PROCEDURE_SCOPE_INVALID",task,versions,run,priority)
            cur.execute(
                """SELECT strategy_id FROM goal_strategy_selections
                   WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                   ORDER BY goal_version DESC LIMIT 1""",
                (variant["goal_id"],user_id,instance_id),
            )
            selected=cur.fetchone()
            if selected is None or selected["strategy_id"]!=variant["strategy_id"]:
                return _outcome("BLOCKED","STRATEGY_CHANGED",task,versions,run,priority)
    cur.execute(
        """SELECT 1 FROM procedure_run_steps
           WHERE run_id=%s AND user_id=%s AND instance_id=%s
             AND executor='AGENT' AND capability=%s
             AND status IN ('NOT_STARTED','FAILED','BLOCKED')
           LIMIT 1""",
        (run["run_id"],user_id,instance_id,task["capability"]),
    )
    if cur.fetchone() is None:
        return _outcome("BLOCKED","STEP_CAPABILITY_MISMATCH",task,versions,run,priority)
    return _outcome("ELIGIBLE","VALIDATED",task,versions,run,priority)


def validate_task_batch(cur,user_id:UUID,instance_id:UUID,
                        after_id:UUID|None,limit:int)->tuple[dict,...]:
    if not 1<=limit<=64:
        raise ValueError("ALIGN-01 task batch must be 1..64")
    # P3 graph owner serializes priority, prerequisites and confirmation
    # updates; there are no model calls inside this short transaction.
    _graph_read_lock(
        cur,TrustedActor(user_id=user_id,source_id="background.alignment"),
        instance_id,
    )
    cur.execute(
        """SELECT * FROM goal_tasks
           WHERE user_id=%s AND instance_id=%s
             AND (%s::uuid IS NULL OR task_id>%s::uuid)
           ORDER BY task_id LIMIT %s FOR SHARE""",
        (user_id,instance_id,after_id,after_id,limit),
    )
    tasks=cur.fetchall()
    return tuple((task,validate_task_alignment(cur,user_id,instance_id,task))
                 for task in tasks)
