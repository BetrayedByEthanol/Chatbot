"""P4-04 Background read-only AGENT step driver.

Runs the existing P0 worker and Procedure-owned result reconciliation. It
introduces neither a queue nor general autonomous model tool permission.
"""
import argparse
import os
import threading

import psycopg

from .background_autonomy import run_autonomous_scan
from .lifecycle import TrustedPlatformActor
from .operation_contracts import StaleOperation
from .operation_control import reap_stale_operations
from .procedure_agent_state import run_one_agent_step, reconcile_agent_step
from .runtime import PermissionDenied, StaleSelection, VersionConflict


def run_autonomous_tick(connection, *, worker_id: str="background-agent"):
    """Bounded new dispatch, one P0-claimed registered read-only execution."""
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
