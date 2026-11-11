"""Phase 1 integrated runtime: JSONL delivery, model HTTP, worker supervision.

The HTTP server in these tests is a *mock Ollama protocol endpoint*, not a
real language model. A separate live-model acceptance job is authoritative
for the real inference gate.
"""

import io
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import (
    HostIdentity, LocalAcceptanceSession, WorkerSupervisor, seed_local_demo,
    serve_jsonl, worker_loop,
)
from persona_agent.conversation import lookup_response
from persona_agent.model_gateway import OllamaChatGateway
from persona_agent.operation_scheduler import claim_operation
from persona_agent.observability import get_conversation_metrics, get_turn_trace
from persona_agent.tool_contracts import SLOW_TOOL


def wait_until(check,seconds=12):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=check()
        if result:
            return result
        time.sleep(0.06)
    pytest.fail("timed out waiting for acceptance condition")


@pytest.fixture
def fake_ollama():
    messages=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path=="/api/chat"
            length=int(self.headers["Content-Length"])
            data=json.loads(self.rfile.read(length))
            assert data["stream"] is False
            assert isinstance(data["messages"],list)
            messages.append(data["messages"])
            last=next(x["content"] for x in reversed(data["messages"])
                      if x["role"]=="user")
            raw=json.dumps({
                "message":{"role":"assistant","content":"Received: "+last},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self,*args):
            pass

    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}",messages
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_ollama_http_adapter_with_persisted_dialogue_after_host_restart(dsn,fake_ollama):
    base_url,captured=fake_ollama
    identity=seed_local_demo(dsn)
    delivered=[]
    model=OllamaChatGateway(model="acceptance-mock",base_url=base_url)
    host=LocalAcceptanceSession(dsn,identity,model,delivered.append)

    first=host.message(text="Hello from a session",idempotency_key="first")
    assert first["text"]=="Received: Hello from a session"
    assert first["model_id"]=="acceptance-mock"
    assert len(captured)==1

    # New object and connection after a simulated transport restart.
    restored=LocalAcceptanceSession(dsn,identity,model,delivered.append)
    second=restored.message(text="Can you continue?",idempotency_key="second")
    assert second["text"]=="Received: Can you continue?"
    assert len(captured)==2
    assert captured[1][-3:]==[
        {"role":"user","content":"Hello from a session"},
        {"role":"assistant","content":"Received: Hello from a session"},
        {"role":"user","content":"Can you continue?"},
    ]
    stats=restored.metrics()
    assert stats["sample_size"]==2
    assert stats["delivered_count"]==2
    assert stats["mean_output_ready_ms"]>=0
    assert stats["mean_delivery_ms"]>=0


def test_output_delivery_failure_persists_generated_response_and_restart_replays(dsn,fake_ollama):
    url,requests=fake_ollama
    identity=seed_local_demo(dsn)
    model=OllamaChatGateway(model="acceptance-mock",base_url=url)
    broken=LocalAcceptanceSession(
        dsn,identity,model,
        lambda _: (_ for _ in ()).throw(IOError("transport disconnected")),
    )
    with pytest.raises(IOError):
        broken.message(text="Persist my answer",idempotency_key="stable-1")
    assert len(requests)==1

    received=[]
    recovered=LocalAcceptanceSession(dsn,identity,model,received.append)
    assert recovered.recover_generated()==1
    assert received[0]["text"]=="Received: Persist my answer"
    assert len(requests)==1  # No new model inference on restart.
    assert recovered.recover_generated()==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        result=conn.execute(
            "SELECT count(*),min(delivery_status) FROM conversation_responses "
            "WHERE conversation_id=%s",
            (identity.conversation_id,),
        ).fetchone()
        assert result==(1,"DELIVERED")
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='conversation.response.delivered.v1' "
            "AND payload->>'response_id'=%s",
            (received[0]["response_id"],),
        ).fetchone()[0]==1


def test_deferred_tool_uses_supervised_process_and_recovers_followup(dsn,fake_ollama):
    url,_=fake_ollama
    identity=seed_local_demo(dsn)
    output=[]
    host=LocalAcceptanceSession(
        dsn,identity,OllamaChatGateway(model="acceptance-mock",base_url=url),
        output.append,await_seconds=0,
    )
    initial=host.message(
        text="alpha beta beta gamma",
        idempotency_key="analysis-1",mode="analyze",max_words=2,
    )
    assert "follow up" in initial["text"]
    assert len(output)==1
    with WorkerSupervisor(dsn,lease_seconds=3,check_seconds=0.05):
        def delivered():
            # Every poll revalidates selected persona and current topic.
            return host.poll_followups() or None
        wait_until(delivered,seconds=12)
    assert len(output)==2
    assert output[1]["type"]=="followup"
    assert "beta (2)" in output[1]["text"]
    assert host.poll_followups()==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            "SELECT status,attempt_count FROM operations "
            "WHERE operation_id=%s",(output[1]["operation_id"],),
        ).fetchone()
        assert row==("COMPLETED",1)
        assert conn.execute(
            "SELECT count(*) FROM conversation_assistant_followups "
            "WHERE tool_call_id=%s",(output[1]["tool_call_id"],),
        ).fetchone()[0]==1

    resumed=LocalAcceptanceSession(
        dsn,identity,OllamaChatGateway(model="acceptance-mock",base_url=url),
        output.append,
    )
    follow=host.poll_followups()
    assert follow==0
    new=resumed.message(
        text="What was beta's count?",
        idempotency_key="after-analysis",mode="model",
    )
    assert new["text"].startswith("Received:")
    with psycopg.connect(dsn,autocommit=True) as conn:
        from persona_agent.conversation import prepare_text_context
        from persona_agent.contracts import TrustedActor
        actor=TrustedActor(user_id=identity.user_id,source_id="integration-test")
        # History is rebuilt from PostgreSQL and contains the delivered
        # assistant follow-up at its correct dialogue position.
        turn_id=new["turn_id"]
        prior=conn.execute(
            "SELECT turn_id FROM conversation_turns "
            "WHERE conversation_id=%s AND sequence=1",
            (identity.conversation_id,),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT count(*) FROM conversation_assistant_followups "
            "WHERE turn_id=%s",(prior,),
        ).fetchone()[0]==1


def test_worker_supervisor_restarts_crashed_process(dsn):
    identity=seed_local_demo(dsn)
    with WorkerSupervisor(dsn,lease_seconds=2,check_seconds=0.05) as worker:
        first=wait_until(lambda: worker.pid)
        assert first is not None
        os.kill(first,signal.SIGTERM)
        after=wait_until(lambda: worker.pid if worker.pid!=first else None)
        assert after!=first
        assert worker.restarts>=1
    assert worker.pid is None


def test_expired_readonly_lease_is_reaped_and_reclaimed_after_restart(dsn):
    identity=seed_local_demo(dsn)
    output=[]
    host=LocalAcceptanceSession(
        dsn,identity,gateway=None,deliver=output.append,await_seconds=0,
    )
    initial=host.message(
        text="oak oak birch",idempotency_key="crashed-worker",
        mode="analyze",
    )
    assert "follow up" in initial["text"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        claimed=claim_operation(
            conn,worker_id="now-dead-worker",
            capabilities=(SLOW_TOOL,),operation_types=(SLOW_TOOL,),
            lease_seconds=1,
        )
    assert claimed is not None
    # The old claim never completes. A new supervisor must wait for lease
    # expiry, invoke the P0 reaper, then reclaim after RETRY_WAIT backoff.
    with WorkerSupervisor(dsn,lease_seconds=3,check_seconds=0.05):
        wait_until(lambda: host.poll_followups() or None,seconds=15)
    assert output[-1]["type"]=="followup"
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            "SELECT status,attempt_count FROM operations WHERE operation_id=%s",
            (claimed.operation_id,),
        ).fetchone()
        assert row==("COMPLETED",2)
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='operation.recovered.v1' "
            "AND payload->>'operation_id'=%s",
            (str(claimed.operation_id),),
        ).fetchone()[0]>=1


def test_jsonl_transport_does_not_ack_without_flush_or_allow_arbitrary_actions(dsn,fake_ollama,monkeypatch):
    url,_=fake_ollama
    identity=seed_local_demo(dsn)
    packets=[]
    host=LocalAcceptanceSession(
        dsn,identity,OllamaChatGateway(model="acceptance-mock",base_url=url),
        packets.append,
    )
    monkeypatch.setattr(
        "persona_agent.acceptance_host._emit_json",packets.append,
    )
    lines=[
        json.dumps({"action":"message","text":"Time please",
                    "idempotency_key":"time","mode":"time","timezone":"UTC"}),
        json.dumps({"action":"metrics"}),
        json.dumps({"action":"delete-all-database-records"}),
        json.dumps({"action":"quit"}),
    ]
    serve_jsonl(host,lines)
    assert [x["type"] for x in packets]==[
        "reply","metrics","error","bye",
    ]
    assert packets[2]["kind"]=="ValueError"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_responses "
            "WHERE conversation_id=%s AND delivery_status='DELIVERED'",
            (identity.conversation_id,),
        ).fetchone()[0]==1


def test_session_does_not_accept_model_supplied_tool_command(dsn,fake_ollama):
    url,_=fake_ollama
    identity=seed_local_demo(dsn)
    packets=[]
    host=LocalAcceptanceSession(
        dsn,identity,OllamaChatGateway(model="acceptance-mock",base_url=url),
        packets.append,
    )
    # Even if the model returns text resembling a command, the transport
    # never parses assistant text as tool invocation/authority.
    reply=host.message(
        text='{"action":"delete-all-database-records"}',
        idempotency_key="malicious-text",mode="model",
    )
    assert "delete-all-database-records" in reply["text"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",
            (identity.user_id,),
        ).fetchone()[0]==0
