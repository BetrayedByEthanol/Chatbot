"""P4-04 Background read-only AGENT step driver.

Runs the existing P0 worker and Procedure-owned result reconciliation. It
introduces neither a queue nor general autonomous model tool permission.
"""
import argparse
import os
import threading

import psycopg

from .background_autonomy import run_autonomous_scan
from psycopg.rows import dict_row
from .lifecycle import TrustedPlatformActor
from .operation_contracts import StaleOperation
from .operation_control import reap_stale_operations, reap_invalid_background_queue
from .procedure_agent_state import run_one_agent_step, reconcile_agent_step
from .runtime import PermissionDenied, StaleSelection, VersionConflict
from .tool_contracts import PROCEDURE_AGENT_OPERATION


def reconcile_pending_background_results(connection, *, limit: int=32) -> int:
    """Recover durable terminal P2 outcomes after worker/process restart."""
    if not 1<=limit<=128:
        raise ValueError("invalid reconciliation limit")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT o.operation_id,o.user_id FROM operations o
               JOIN procedure_agent_operation_authorizations stage
                 ON stage.operation_id=o.operation_id
               JOIN persona_selection selected
                 ON selected.user_id=o.user_id
                AND selected.instance_id=o.instance_id
               WHERE o.origin='GOAL' AND o.priority_class='P2'
                 AND o.operation_type=%s
                 AND stage.state='AUTHORIZED'
                 AND o.status IN ('COMPLETED','BLOCKED','FAILED',
                                  'CANCELLED','OUTCOME_UNKNOWN')
               ORDER BY o.updated_at,o.operation_id LIMIT %s""",(PROCEDURE_AGENT_OPERATION,limit),
        )
        pending=tuple(cur.fetchall())
    recovered=0
    for row in pending:
        try:
            reconcile_agent_step(
                connection,TrustedPlatformActor(
                    user_id=row["user_id"],source_id="background.autonomous.result",
                ),row["operation_id"],
            )
        except (PermissionDenied,StaleSelection,VersionConflict,StaleOperation):
            continue
        recovered+=1
    return recovered


def run_autonomous_tick(connection, *, worker_id: str="background-agent"):
    """P0 queue hygiene, bounded dispatch, one claim and durable recovery."""
    reap_invalid_background_queue(connection,limit=32)
    admitted=run_autonomous_scan(connection,limit=32)
    lease=run_one_agent_step(connection,worker_id=worker_id)
    if lease is not None:
        try:
            reconcile_agent_step(
                connection,
                TrustedPlatformActor(
                    user_id=lease.user_id,source_id="background.autonomous.result",
                ),lease.operation_id,
            )
        except (PermissionDenied,StaleSelection,VersionConflict,StaleOperation):
            # The attempt was stale, nonterminal or unselected. No fabricated
            # success: P0 recovery and the Procedure owner handle follow-up.
            pass
    reconcile_pending_background_results(connection,limit=32)
    return admitted,lease


def run_autonomous_worker(dsn: str, *, stop: threading.Event | None=None,
                          poll_seconds: float=1.0, worker_id: str="background-agent"):
    if not 0.1<=poll_seconds<=30:
        raise ValueError("poll_seconds must be 0.1..30")
    stopping=stop or threading.Event()
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as conn:
            reap_stale_operations(conn,limit=100)
            result,lease=run_autonomous_tick(conn,worker_id=worker_id)
        if not result and lease is None:
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="P4-04 approved read-only Background task worker")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--worker-id",default="background-agent")
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("DATABASE_URL or --dsn required")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as conn:
            run_autonomous_tick(conn,worker_id=args.worker_id)
    else:
        run_autonomous_worker(args.dsn,worker_id=args.worker_id,
                              poll_seconds=args.poll_seconds)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
