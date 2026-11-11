"""Phase 1 local-only acceptance transport and supervised read-only worker.

This is an *operator-owned* JSON-lines stdin/stdout harness, not an
authenticated HTTP server. It exercises the real domain contracts without
adding a web framework, modifying the scheduler, or granting model/tool
output any additional authority. Each DB operation uses a fresh autocommit
connection. Output acknowledgments happen only after the channel confirms
a successful write/flush; duplicate sends remain possible across crashes.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import (
    acknowledge_text_delivery, ingest_text, respond_to_text,
)
from .conversation_contracts import ModelMessage, ModelReply, TextInputCommand
from .model_gateway import OllamaChatGateway
from .observability import get_conversation_metrics
from .operation_control import reap_stale_operations
from .operation_scheduler import configure_resource_pool
from .tool_contracts import RESOURCE_KEY, SlowTextToolCommand
from .tool_gateway import (
    acknowledge_tool_followup, prepare_tool_followup,
    reconcile_tool_followup, respond_to_text_analysis, respond_with_time,
    run_one_text_tool,
)


@dataclass(frozen=True)
class HostIdentity:
    """Provided by the trusted local operator, never inferred from user text."""

    user_id: UUID
    instance_id: UUID
    conversation_id: UUID
    generation: int

    def __post_init__(self):
        if self.generation < 1:
            raise ValueError("generation must be positive")


class _ReplayOnly:
    def generate(self, messages):
        raise AssertionError("recovery must not call the model")


class LocalAcceptanceSession:
    def __init__(
        self, dsn: str, identity: HostIdentity,
        gateway,
        deliver: Callable[[dict], None],
        *, await_seconds: float = 0.05,
        source_id: str = "local-acceptance-stdin",
    ):
        if not 0 <= await_seconds <= 5:
            raise ValueError("await_seconds must be 0..5")
        if not 1 <= len(source_id) <= 128:
            raise ValueError("invalid source_id")
        self.dsn=dsn
        self.identity=identity
        self.gateway=gateway
        self.deliver=deliver
        self.await_seconds=await_seconds
        self.actor=TrustedActor(user_id=identity.user_id,source_id=source_id)

    def _connection(self):
        return psycopg.connect(self.dsn,autocommit=True)

    def _deliver_response(self, turn_id: UUID, response):
        # This is a fresh authorization check, close to the external output
        # boundary, without regenerating or otherwise changing the response.
        with self._connection() as conn:
            approved=respond_to_text(conn,self.actor,turn_id,_ReplayOnly())
        if approved.response_id!=response.response_id:
            raise RuntimeError("response authority changed before delivery")
        payload={
            "type":"reply","turn_id":str(turn_id),
            "response_id":str(response.response_id),
            "model_id":response.model_id,"text":response.text,
            "replayed":response.replayed,
        }
        self.deliver(payload)  # channel writes and flushes before returning
        with self._connection() as conn:
            acknowledge_text_delivery(conn,self.actor,response.response_id)
        return payload

    def message(
        self, *, text: str, idempotency_key: str,
        mode: str="model", timezone_name: str="UTC",
        max_words: int=5,
    ) -> dict:
        if mode not in ("model","time","analyze"):
            raise ValueError("unknown message mode")
        with self._connection() as conn:
            entered=ingest_text(
                conn,self.actor,
                TextInputCommand(
                    conversation_id=self.identity.conversation_id,
                    instance_id=self.identity.instance_id,
                    expected_generation=self.identity.generation,
                    idempotency_key=idempotency_key,
                    text=text,
                ),
            )
        with self._connection() as conn:
            if mode=="time":
                reply=respond_with_time(
                    conn,self.actor,entered.turn_id,timezone_name=timezone_name,
                )
            elif mode=="analyze":
                reply=respond_to_text_analysis(
                    conn,self.actor,
                    SlowTextToolCommand(turn_id=entered.turn_id,max_words=max_words),
                    await_seconds=self.await_seconds,
                )
            else:
                reply=respond_to_text(conn,self.actor,entered.turn_id,self.gateway)
        return self._deliver_response(entered.turn_id,reply)

    def recover_generated(self) -> int:
        """Replay only saved GENERATED responses, never invent a lost route."""
        with self._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT r.turn_id FROM conversation_responses r
                    JOIN conversations c ON c.conversation_id=r.conversation_id
                    WHERE r.conversation_id=%s AND r.user_id=%s
                      AND r.instance_id=%s AND r.delivery_status='GENERATED'
                      AND c.last_input_turn_id=r.turn_id
                    ORDER BY r.generated_at,r.response_id
                    """,
                    (self.identity.conversation_id,self.identity.user_id,
                     self.identity.instance_id),
                )
                pending=[r["turn_id"] for r in cur.fetchall()]
        count=0
        for turn_id in pending:
            with self._connection() as conn:
                approved=respond_to_text(conn,self.actor,turn_id,_ReplayOnly())
            self._deliver_response(turn_id,approved)
            count+=1
        return count

    def poll_followups(self, *, limit: int=20) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("poll limit must be 1..100")
        with self._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT tool_call_id FROM conversation_tool_calls
                    WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                      AND followup_state IN ('PENDING','READY','DEFERRED')
                    ORDER BY created_at,tool_call_id LIMIT %s
                    """,
                    (self.identity.conversation_id,self.identity.user_id,
                     self.identity.instance_id,limit),
                )
                ids=[r["tool_call_id"] for r in cur.fetchall()]
        sent=0
        for tool_id in ids:
            with self._connection() as conn:
                state=reconcile_tool_followup(conn,self.actor,tool_id)
            if state.followup_state!="READY":
                continue
            with self._connection() as conn:
                result=prepare_tool_followup(conn,self.actor,tool_id)
            self.deliver({
                "type":"followup","tool_call_id":str(tool_id),
                "operation_id":str(result.operation_id),"text":result.text,
            })
            with self._connection() as conn:
                acknowledge_tool_followup(conn,self.actor,tool_id)
            sent+=1
        return sent

    def metrics(self) -> dict:
        with self._connection() as conn:
            return get_conversation_metrics(
                conn,self.actor,self.identity.conversation_id,
            ).model_dump(mode="json")


def worker_loop(
    dsn: str, *, stop: threading.Event | None=None,
    poll_seconds: float=0.1,
    lease_seconds: int=10,
    worker_id: str | None=None,
    recover_every_seconds: float=2.0,
    executor=None,
) -> None:
    """One durable P0 worker process; connection and tool effects are separate.

    After a process crash, a new worker reaps expired leases, then the
    scheduler decides if an operation may be reclaimed. It never bypasses
    attempt caps, persona generation or the effect confirmation fence.
    """
    if not 0.01 <= poll_seconds <= 10:
        raise ValueError("poll_seconds must be 0.01..10")
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be 1..3600")
    if recover_every_seconds<=0:
        raise ValueError("recover interval must be positive")
    stopping=stop or threading.Event()
    worker_id=worker_id or f"acceptance-{os.getpid()}-{uuid4().hex[:10]}"
    last_reap=float("-inf")
    while not stopping.is_set():
        with psycopg.connect(dsn,autocommit=True) as conn:
            if time.monotonic()-last_reap>=recover_every_seconds:
                reap_stale_operations(conn,limit=100)
                last_reap=time.monotonic()
            kwargs={"worker_id":worker_id,"lease_seconds":lease_seconds}
            if executor is not None:
                kwargs["executor"]=executor
            lease=run_one_text_tool(conn,**kwargs)
        if lease is None:
            stopping.wait(poll_seconds)


class WorkerSupervisor:
    """Supervise one subprocess; bounded restarts, no daemon framework."""

    def __init__(self,dsn: str, *, poll_seconds: float=0.1,
                 lease_seconds: int=10,check_seconds: float=0.25):
        self.dsn=dsn
        self.poll_seconds=poll_seconds
        self.lease_seconds=lease_seconds
        self.check_seconds=check_seconds
        self._stop=threading.Event()
        self._lock=threading.Lock()
        self._process: subprocess.Popen | None=None
        self._monitor: threading.Thread | None=None
        self._attempts: list[float]=[]
        self.restarts=0

    def start(self) -> None:
        if self._monitor is not None:
            raise RuntimeError("supervisor already started")
        self._monitor=threading.Thread(
            target=self._watch,name="acceptance-worker-supervisor",daemon=True,
        )
        self._monitor.start()

    def _watch(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._process is None or self._process.poll() is not None:
                    now=time.monotonic()
                    self._attempts=[t for t in self._attempts if now-t<60]
                    if len(self._attempts)>=5:
                        # A permanently broken worker must not spin forever.
                        self._stop.set()
                        break
                    self._attempts.append(now)
                    env=os.environ.copy()
                    env["ACCEPTANCE_DATABASE_URL"]=self.dsn
                    cmd=[
                        sys.executable,"-m","persona_agent.acceptance_host",
                        "worker","--poll-seconds",str(self.poll_seconds),
                        "--lease-seconds",str(self.lease_seconds),
                    ]
                    self._process=subprocess.Popen(
                        cmd,env=env,stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,stderr=sys.stderr,
                    )
                    if len(self._attempts)>1:
                        self.restarts+=1
            self._stop.wait(self.check_seconds)

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process and self._process.poll() is None else None

    def stop(self) -> None:
        self._stop.set()
        if self._monitor:
            self._monitor.join(timeout=2)
        with self._lock:
            child=self._process
            self._process=None
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self,*exc):
        self.stop()


def seed_local_demo(dsn: str) -> HostIdentity:
    """Operator-only fixture provisioning; NEVER an untrusted transport verb."""
    user,persona,instance,conversation=(uuid4() for _ in range(4))
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"local-acceptance-{persona}"),
            )
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES (%s,%s,%s,'Acceptance Demo')",
                (instance,user,persona),
            )
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')",(user,instance),
            )
        configure_resource_pool(conn,RESOURCE_KEY,capacity_units=2)
    return HostIdentity(user,instance,conversation,1)


def _emit_json(obj: dict) -> None:
    print(json.dumps(obj,ensure_ascii=False),flush=True)


def serve_jsonl(session: LocalAcceptanceSession, lines) -> None:
    """Text transport; typed actions, never execute a command from model text."""
    for line in lines:
        if not line.strip():
            continue
        try:
            packet=json.loads(line)
            if not isinstance(packet,dict):
                raise ValueError("packet must be an object")
            action=packet.get("action")
            if action=="message":
                session.message(
                    text=packet["text"],
                    idempotency_key=packet["idempotency_key"],
                    mode=packet.get("mode","model"),
                    timezone_name=packet.get("timezone","UTC"),
                    max_words=packet.get("max_words",5),
                )
            elif action=="recover":
                _emit_json({"type":"recovered","count":session.recover_generated()})
            elif action=="poll":
                _emit_json({"type":"polled","count":session.poll_followups()})
            elif action=="metrics":
                _emit_json({"type":"metrics","data":session.metrics()})
            elif action=="quit":
                _emit_json({"type":"bye"})
                break
            else:
                raise ValueError("unknown action")
        except (KeyError,ValueError,TypeError) as exc:
            _emit_json({"type":"error","kind":type(exc).__name__})
        # Operational errors intentionally propagate (fail closed). A local
        # operator can retry with the same stable idempotency key on restart.


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(
        description="Local-only, explicitly trusted Phase 1 acceptance harness",
    )
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("seed",help="provision disposable demo principal")
    worker=sub.add_parser("worker",help="run one supervised-capable durable worker")
    worker.add_argument("--poll-seconds",type=float,default=0.1)
    worker.add_argument("--lease-seconds",type=int,default=10)
    serve=sub.add_parser("serve",help="JSONL stdin/stdout; trusted local operator")
    for name in ("user-id","instance-id","conversation-id"):
        serve.add_argument("--"+name,required=True,type=UUID)
    serve.add_argument("--generation",type=int,default=1)
    serve.add_argument("--model",default="qwen2.5:0.5b")
    serve.add_argument("--ollama-url",default="http://127.0.0.1:11434")
    serve.add_argument("--await-seconds",type=float,default=0.05)
    serve.add_argument("--no-worker",action="store_true")
    args=parser.parse_args(argv)
    dsn=os.environ.get("ACCEPTANCE_DATABASE_URL")
    if not dsn:
        parser.error("ACCEPTANCE_DATABASE_URL is required")
    if args.command=="seed":
        identity=seed_local_demo(dsn)
        _emit_json({"type":"identity","user_id":str(identity.user_id),
                    "instance_id":str(identity.instance_id),
                    "conversation_id":str(identity.conversation_id),
                    "generation":identity.generation})
        return 0
    if args.command=="worker":
        worker_loop(
            dsn,poll_seconds=args.poll_seconds,lease_seconds=args.lease_seconds,
        )
        return 0
    identity=HostIdentity(
        args.user_id,args.instance_id,args.conversation_id,args.generation,
    )
    model=OllamaChatGateway(model=args.model,base_url=args.ollama_url,
                            timeout_seconds=180)
    session=LocalAcceptanceSession(
        dsn,identity,model,_emit_json,await_seconds=args.await_seconds,
    )
    if args.no_worker:
        serve_jsonl(session,sys.stdin)
    else:
        with WorkerSupervisor(dsn):
            serve_jsonl(session,sys.stdin)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
