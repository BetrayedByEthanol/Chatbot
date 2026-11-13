"""ALIGN-01 epoch assessment and atomic P0 readiness certification.

The Evaluation component coordinates *read-only* Goal/Task/Procedure owner
projections; only the Background coordinator may invoke it while holding
selection FOR SHARE and epoch FOR UPDATE. No generic cognitive proposal may
issue domain commands or grant P2 authority.
"""
import json
from uuid import UUID

from .alignment_owner_checks import validate_task_batch


def process_alignment(cur,epoch:dict)->str:
    """One bounded SQL-only ALIGN-01 pass. Caller owns the authority locks."""
    scope=(epoch["user_id"],epoch["instance_id"],epoch["background_epoch"])
    cur.execute(
        """SELECT count(*) AS n,count(*) FILTER(WHERE
                c.summary_watermark>=b.target_sequence
                AND c.cognitive_watermark>=b.target_sequence
                AND c.critical_committed_watermark>=b.target_sequence
                AND b.critical_scan_sequence=b.target_sequence) AS complete
           FROM background_preparation_conversations b
           JOIN conversations c ON c.conversation_id=b.conversation_id
             AND c.user_id=b.user_id AND c.instance_id=b.instance_id
           WHERE b.user_id=%s AND b.instance_id=%s AND b.background_epoch=%s""",
        scope,
    )
    bounds=cur.fetchone()
    if (bounds["n"]!=epoch["captured_conversation_count"]
        or bounds["complete"]!=bounds["n"]):
        return "SOURCE_UNVERIFIED"
    cur.execute(
        """SELECT 1 FROM background_critical_updates
           WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
             AND status='PENDING' LIMIT 1""",scope,
    )
    if cur.fetchone() is not None:
        return "CRITICAL_UNRESOLVED"
    # The initial pass reviews existing AGENT work too. Later epochs do a
    # cheap, version-pinned revalidation rather than a full-state LLM audit.
    tasks=validate_task_batch(
        cur,epoch["user_id"],epoch["instance_id"],
        epoch["alignment_last_task_id"],32,
    )
    if tasks:
        for task,review in tasks:
            cur.execute(
                """INSERT INTO background_alignment_task_checks(
                     user_id,instance_id,background_epoch,task_id,
                     task_version,goal_id,goal_versions,procedure_run_id,
                     procedure_run_version,decision,reason,priority_rank
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
                   ON CONFLICT (user_id,instance_id,background_epoch,task_id)
                   DO NOTHING""",
                (*scope,task["task_id"],review["task_version"],
                 review["goal_id"],json.dumps(review["goal_versions"]),
                 review["procedure_run_id"],review["procedure_run_version"],
                 review["decision"],review["reason"],review["priority_rank"]),
            )
        cur.execute(
            """UPDATE background_preparation_epochs
               SET alignment_last_task_id=%s,
                   alignment_scanned_count=alignment_scanned_count+%s,
                   preparation_updated_at=clock_timestamp()
               WHERE user_id=%s AND instance_id=%s AND background_epoch=%s
                 AND preparation_stage='ALIGNMENT_PENDING' AND status='PENDING'""",
            (tasks[-1][0]["task_id"],len(tasks),*scope),
        )
        if cur.rowcount!=1:
            return "SOURCE_UNVERIFIED"
        return "SCANNING"

    cur.execute(
        """SELECT count(*) AS n FROM goal_tasks
           WHERE user_id=%s AND instance_id=%s""",
        (epoch["user_id"],epoch["instance_id"]),
    )
    actual=cur.fetchone()["n"]
    if actual!=epoch["alignment_scanned_count"]:
        return "SOURCE_UNVERIFIED"
    cur.execute(
        """SELECT count(*) AS n,
                  count(*) FILTER(WHERE decision='ELIGIBLE') AS eligible,
                  count(*) FILTER(WHERE decision='BLOCKED') AS blocked,
                  count(*) FILTER(WHERE decision='REVIEW') AS review
           FROM background_alignment_task_checks
           WHERE user_id=%s AND instance_id=%s AND background_epoch=%s""",
        scope,
    )
    status=cur.fetchone()
    if status["n"]!=actual:
        return "SOURCE_UNVERIFIED"
    # This is a multi-tick scan. Final certification must reject an owner
    # revision committed after its earlier task batch was validated.
    # Frozen Goal ancestry, procedure/run and task versions all matter.
    cur.execute(
        """SELECT 1 FROM background_alignment_task_checks checked
           JOIN goal_tasks task
             ON task.task_id=checked.task_id
            AND task.user_id=checked.user_id
            AND task.instance_id=checked.instance_id
           LEFT JOIN procedure_runs run
             ON run.run_id=checked.procedure_run_id
            AND run.user_id=checked.user_id
            AND run.instance_id=checked.instance_id
           WHERE checked.user_id=%s AND checked.instance_id=%s
             AND checked.background_epoch=%s
             AND (task.version<>checked.task_version
               OR (checked.procedure_run_id IS NOT NULL
                   AND (run.run_id IS NULL
                        OR run.version<>checked.procedure_run_version))
               OR EXISTS (
                   SELECT 1 FROM jsonb_each_text(checked.goal_versions) AS pinned(id,ver)
                   LEFT JOIN goal_records goal
                     ON goal.goal_id=pinned.id::uuid
                    AND goal.user_id=checked.user_id
                    AND goal.instance_id=checked.instance_id
                   WHERE goal.goal_id IS NULL
                      OR goal.version<>pinned.ver::bigint
               ))
           LIMIT 1""",scope,
    )
    if cur.fetchone() is not None:
        return "SOURCE_UNVERIFIED"
    # Audit the precise result, NOT a generic "alignment complete" boolean.
    # The certificate and the existing P0 ready row share one transaction
    # with the preparation stage event/outbox in the calling coordinator.
    cur.execute(
        """INSERT INTO background_alignment_certificates(
             user_id,instance_id,background_epoch,selection_generation,
             checked_tasks,eligible_tasks,blocked_tasks,review_tasks
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING""",
        (*scope,epoch["selection_generation"],status["n"],
         status["eligible"],status["blocked"],status["review"]),
    )
    cur.execute(
        """INSERT INTO background_ready_epochs(
             user_id,instance_id,background_epoch
           ) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",scope,
    )
    return "READY"
