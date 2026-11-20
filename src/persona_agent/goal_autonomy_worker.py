"""Recovering standalone P2 Goal evaluator; uses the existing P0 owner."""
import argparse
import logging
import os
import threading
import psycopg

from .conversation_contracts import ModelUnavailable
from .goal_autonomy import run_goal_evaluation_scan,run_one_goal_evaluation
from .model_gateway import OllamaChatGateway
from .operation_contracts import StaleOperation
from .operation_control import reap_stale_operations,reap_invalid_background_queue
from .runtime import PermissionDenied,StaleSelection,VersionConflict

_LOG=logging.getLogger(__name__)


def run_goal_evaluation_tick(connection,gateway,*,worker_id="goal-autonomy"):
    # A standalone worker cannot assume another scheduler will recover
    # orphaned claims. Reap expired leases before the next bounded scan.
    reap_stale_operations(connection,limit=24)
    reap_invalid_background_queue(connection,limit=24)
    staged=run_goal_evaluation_scan(connection,limit=24)
    try:
        executed=run_one_goal_evaluation(connection,gateway,worker_id=worker_id)
    except (ModelUnavailable,ValueError,StaleOperation,
            StaleSelection,PermissionDenied,VersionConflict) as exc:
        # run_goal_evaluation already failed a live attempt through P0;
        # invalidated attempts are reconciled by the reaper on next tick.
        _LOG.warning("Goal evaluation attempt deferred: %s",type(exc).__name__)
        executed=None
    return staged,executed


def run_goal_evaluation_worker(
    dsn:str,gateway,*,stop:threading.Event|None=None,
    poll_seconds:float=1.0,worker_id:str="goal-autonomy",
):
    if not 0.1<=poll_seconds<=30.0:
        raise ValueError("poll_seconds must be in 0.1..30")
    stopping=stop or threading.Event()
    while not stopping.is_set():
        try:
            with psycopg.connect(dsn,autocommit=True) as connection:
                staged,executed=run_goal_evaluation_tick(
                    connection,gateway,worker_id=worker_id,
                )
        except Exception:
            # A transient DB or unexpected model/provider failure must not
            # permanently terminate this bounded Background worker.
            _LOG.exception("Goal evaluation tick failed")
            stopping.wait(poll_seconds)
            continue
        if executed is None:
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Bounded P2 Goal evaluator")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--model",default=os.getenv("OLLAMA_MODEL","llama3.2:1b"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    opts=parser.parse_args(argv)
    if not opts.dsn:
        parser.error("--dsn or DATABASE_URL required")
    logging.basicConfig(level=logging.INFO)
    run_goal_evaluation_worker(
        opts.dsn,OllamaChatGateway(model=opts.model),
        poll_seconds=opts.poll_seconds,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
