"""P4-06 deterministic platform notifier worker: in-app inbox only.

No persona selection, model/LLM, Goal/Task mutation, or external provider.
The worker only advances due authorized communication intents atomically.
"""
import argparse
import os
import threading

import psycopg

from .notification_state import dispatch_due_notifications


def run_notification_worker(
    dsn: str, *, stop: threading.Event | None=None,
    poll_seconds: float=1.0,batch_limit: int=32,
) -> None:
    if not 0.1<=poll_seconds<=60:
        raise ValueError("poll_seconds must be 0.1..60")
    if not 1<=batch_limit<=128:
        raise ValueError("batch_limit must be 1..128")
    stopping=stop or threading.Event()
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as conn:
            processed=dispatch_due_notifications(conn,limit=batch_limit)
        if processed==0:
            stopping.wait(poll_seconds)


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="P4-06 in-app notification platform")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--batch-limit",type=int,default=32)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("DATABASE_URL or --dsn required")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as conn:
            dispatch_due_notifications(conn,limit=args.batch_limit)
    else:
        run_notification_worker(args.dsn,poll_seconds=args.poll_seconds,
                                batch_limit=args.batch_limit)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
