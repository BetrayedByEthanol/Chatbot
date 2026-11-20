"""MVP-03A.4 bounded Goal evaluation loop: stage via READY, claim via P0."""
import argparse
import os
import threading
import psycopg

from .goal_autonomy import run_goal_evaluation_scan,run_one_goal_evaluation
from .model_gateway import OllamaChatGateway


def run_goal_evaluation_tick(connection,gateway,*,worker_id="goal-autonomy"):
    staged=run_goal_evaluation_scan(connection,limit=24)
    executed=run_one_goal_evaluation(connection,gateway,worker_id=worker_id)
    return staged,executed


def run_goal_evaluation_worker(
    dsn:str,gateway,*,stop:threading.Event|None=None,
    poll_seconds:float=1.0,worker_id:str="goal-autonomy",
):
    if not 0.1<=poll_seconds<=30.0:
        raise ValueError("poll_seconds must be in 0.1..30")
    stopping=stop or threading.Event()
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as connection:
            staged,executed=run_goal_evaluation_tick(
                connection,gateway,worker_id=worker_id,
            )
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
    run_goal_evaluation_worker(
        opts.dsn,OllamaChatGateway(model=opts.model),
        poll_seconds=opts.poll_seconds,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
