"""Operator entry point for P4-02 SQL-only Background coordination.

Run alongside existing P0 summary and cognition workers; this worker does not
invoke a model, own a resource pool or grant autonomous execution authority.
"""

import argparse
import os
import threading

import psycopg

from .background_coordinator import run_preparation_scan


def run_preparation_worker(
    dsn: str, *, stop: threading.Event | None = None,
    poll_seconds: float = 1.0, limit: int = 32,
) -> None:
    if not 0.1 <= poll_seconds <= 30:
        raise ValueError("poll_seconds must be 0.1..30")
    stopping=stop or threading.Event()
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as connection:
            results=run_preparation_scan(connection,limit=limit)
        if not any(result.state in ("ENQUEUED","STAGE_ADVANCED") for result in results):
            stopping.wait(poll_seconds)


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="P4-02 Background preparation coordinator")
    parser.add_argument("--dsn",default=os.getenv("DATABASE_URL"))
    parser.add_argument("--poll-seconds",type=float,default=1.0)
    parser.add_argument("--limit",type=int,default=32)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("database URL required via --dsn or DATABASE_URL")
    if args.once:
        with psycopg.connect(args.dsn,autocommit=True) as connection:
            run_preparation_scan(connection,limit=args.limit)
    else:
        run_preparation_worker(args.dsn,poll_seconds=args.poll_seconds,limit=args.limit)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
