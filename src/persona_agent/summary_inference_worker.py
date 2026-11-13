"""P0-owned summary inference runner for scheduled P2/P4 summary operations.

The existing automatic_summaries worker only admits operations. This runner
executes them through P0 claims; it creates no queue or new model scheduler.
"""

import argparse
import os
import threading

import psycopg

from .conversation_contracts import ModelUnavailable
from .model_gateway import OllamaChatGateway
from .operation_contracts import StaleOperation
from .operation_control import fail_operation, reap_stale_operations
from .operation_scheduler import claim_operation
from .summary_contracts import (
    InvalidSummary, StaleSummary, SummaryNotReady,
    SUMMARY_CAPABILITY, SUMMARY_OPERATION,
)
from .summary_memory import run_summary


def run_one_summary_operation(connection, gateway, *, worker_id: str):
    """Claim and complete at most one P0 summary operation."""
    lease=claim_operation(
        connection,worker_id=worker_id,
        operation_types=(SUMMARY_OPERATION,),
        capabilities=(SUMMARY_CAPABILITY,),lease_seconds=120,
    )
    if lease is None:
        return None
    try:
        return run_summary(connection,lease,gateway)
    except ModelUnavailable:
        # The P2 summary primitive handles InvalidSummary itself. A transport
        # failure happens before its commit and must still release the P0 claim.
        try:
            fail_operation(connection,lease,reason="summary model unavailable",
                           delay_seconds=5)
        except StaleOperation:
            pass
        raise


def run_summary_inference_worker(
    dsn: str, *, model: str, poll_seconds: float = 1.0,
    worker_id: str = "summary-inference",
    stop: threading.Event | None = None,
) -> None:
    if not 0.1 <= poll_seconds <= 30:
        raise ValueError("poll_seconds must be 0.1..30")
    gateway=OllamaChatGateway(model=model)
    stopping=stop or threading.Event()
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as connection:
            reap_stale_operations(connection,limit=100)
            try:
                outcome=run_one_summary_operation(
                    connection,gateway,worker_id=worker_id,
                )
            except (InvalidSummary,ModelUnavailable,StaleOperation,
                    StaleSummary,SummaryNotReady):
                outcome=None
        if outcome is None:
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="P0-backed P2/P4 summary inference")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--model",default=os.getenv("SUMMARY_MODEL"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--worker-id",default="summary-inference")
    args=parser.parse_args(argv)
    if not args.dsn or not args.model:
        parser.error("DATABASE_URL and SUMMARY_MODEL (or --dsn/--model) required")
    run_summary_inference_worker(
        args.dsn,model=args.model,poll_seconds=args.poll_seconds,
        worker_id=args.worker_id,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
