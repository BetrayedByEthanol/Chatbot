#!/usr/bin/env python3
"""Phase 1 live acceptance against an actually running Ollama model.

Exit nonzero if any gate fails. This is NOT the mocked HTTP protocol test.
Required: applied migrations, a PostgreSQL DSN and a pulled Ollama model.
Output intentionally contains metrics and IDs only, never raw user text.
"""

import json
import os
import subprocess
import sys
import time

import psycopg

from persona_agent.acceptance_host import (
    LocalAcceptanceSession, WorkerSupervisor, seed_local_demo,
)
from persona_agent.model_gateway import OllamaChatGateway
from persona_agent.tool_contracts import SLOW_TOOL


def ensure(condition: bool, reason: str):
    if not condition:
        raise AssertionError(reason)


def main():
    dsn=os.environ["ACCEPTANCE_DATABASE_URL"]
    model_id=os.environ.get("ACCEPTANCE_MODEL","qwen2.5:0.5b")
    endpoint=os.environ.get("ACCEPTANCE_OLLAMA_URL","http://127.0.0.1:11434")
    identity=seed_local_demo(dsn)
    gateway=OllamaChatGateway(
        model=model_id,base_url=endpoint,timeout_seconds=180,
    )
    received=[]
    session=LocalAcceptanceSession(
        dsn,identity,gateway,received.append,await_seconds=0,
    )

    start=time.monotonic()
    first=session.message(
        text="Reply briefly: hello.",idempotency_key="live-model-1",
    )
    model_latency_ms=round((time.monotonic()-start)*1000,1)
    ensure(first["model_id"]==model_id,"configured model was not used")
    ensure(bool(first["text"].strip()),"model returned no text")

    # Real model answer persisted but transport delivery failed. Recreate
    # the session; the next attempt MUST only replay the saved answer.
    def disconnected(_):
        raise OSError("synthetic output channel failure")

    previous=LocalAcceptanceSession(dsn,identity,gateway,disconnected)
    try:
        previous.message(
            text="Reply briefly: second message.",
            idempotency_key="live-model-2",
        )
    except OSError:
        pass
    else:
        raise AssertionError("disconnected transport unexpectedly delivered")
    recovered=LocalAcceptanceSession(dsn,identity,gateway,received.append)
    ensure(recovered.recover_generated()==1,"saved model answer was not recovered")
    ensure(recovered.recover_generated()==0,"delivery was not idempotently acknowledged")

    clock=recovered.message(
        text="What time is it in UTC?",
        idempotency_key="live-clock",
        mode="time",timezone_name="UTC",
    )
    ensure(clock["model_id"]=="clock.now.v1","immediate tool not used")

    notice=recovered.message(
        text="alpha beta beta gamma",
        idempotency_key="live-slow",
        mode="analyze",max_words=2,
    )
    ensure("follow up" in notice["text"],"slow tool was not deferred")

    with WorkerSupervisor(dsn,lease_seconds=10,check_seconds=0.1):
        end=time.monotonic()+35
        while time.monotonic()<end:
            if recovered.poll_followups():
                break
            time.sleep(0.15)
        else:
            raise AssertionError("supervised worker did not deliver tool follow-up")

    follow=[m for m in received if m["type"]=="followup"]
    ensure(len(follow)==1,"exactly one slow follow-up expected")
    ensure("beta (2)" in follow[0]["text"],"wrong slow-tool result")
    ensure(recovered.poll_followups()==0,"duplicate follow-up offered")

    stats=recovered.metrics()
    ensure(stats["sample_size"]==4,"expected four durable text turns")
    ensure(stats["delivered_count"]==4,"all initial replies must be delivered")
    ensure(stats["active_tool_count"]==0,"worker left an active operation")
    ensure(stats["mean_output_ready_ms"] is not None,"output latency was not recorded")
    with psycopg.connect(dsn,autocommit=True) as conn:
        operation=conn.execute(
            "SELECT operation_type,status,attempt_count "
            "FROM operations WHERE operation_id=%s",
            (follow[0]["operation_id"],),
        ).fetchone()
        ensure(operation==(SLOW_TOOL,"COMPLETED",1),
               "slow tool was not durably completed")
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses "
            "WHERE conversation_id=%s AND delivery_status='DELIVERED'",
            (identity.conversation_id,),
        ).fetchone()[0]==4

    # Exercise the actual interactive text host process, not only its class.
    pipe_identity=seed_local_demo(dsn)
    cmd=[
        sys.executable,"-m","persona_agent.acceptance_host","serve",
        "--user-id",str(pipe_identity.user_id),
        "--instance-id",str(pipe_identity.instance_id),
        "--conversation-id",str(pipe_identity.conversation_id),
        "--generation",str(pipe_identity.generation),
        "--model",model_id,"--ollama-url",endpoint,"--no-worker",
    ]
    packets=[
        {"action":"message","mode":"model","idempotency_key":"pipe-model",
         "text":"Say a short greeting."},
        {"action":"message","mode":"time","idempotency_key":"pipe-clock",
         "text":"What time is it?","timezone":"UTC"},
        {"action":"metrics"},{"action":"quit"},
    ]
    child=subprocess.run(
        cmd,input="".join(json.dumps(item)+"\n" for item in packets),
        capture_output=True,text=True,timeout=200,
        env={**os.environ,"ACCEPTANCE_DATABASE_URL":dsn},check=True,
    )
    outputs=[json.loads(line) for line in child.stdout.splitlines() if line.strip()]
    ensure([p["type"] for p in outputs]==["reply","reply","metrics","bye"],
           "real JSON-lines host did not complete")
    ensure(outputs[0]["model_id"]==model_id and bool(outputs[0]["text"].strip()),
           "real model did not reply through text host")
    ensure(outputs[1]["model_id"]=="clock.now.v1",
           "immediate tool did not reply through text host")
    ensure(outputs[2]["data"]["delivered_count"]==2,
           "text host did not persist delivery")

    print(json.dumps({
        "phase":"phase1-integrated-acceptance",
        "real_model_used":True,
        "model":model_id,
        "model_call_ms":model_latency_ms,
        "generated_recovery":"passed",
        "immediate_tool":"passed",
        "supervised_deferred_tool":"passed",
        "followup_exactly_once_ack":"passed",
        "real_jsonl_subprocess_transport":"passed",
        "turns":stats["sample_size"],
        "output_ready_p95_ms":stats["p95_output_ready_ms"],
        "delivery_p95_ms":stats["p95_delivery_ms"],
    },sort_keys=True))


if __name__=="__main__":
    main()
