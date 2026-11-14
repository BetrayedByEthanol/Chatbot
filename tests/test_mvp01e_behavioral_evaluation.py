"""MVP-01E: non-destructive scenario validation, evidence and real SQL tests."""
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile
import json

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.behavioral_evaluation import (
    RecordingGateway, capture_state, run_scenario,
    validate_scenario, write_bundle,
)
from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation_contracts import ModelReply
from persona_agent.tool_contracts import TIME_TOOL


class SimpleModel:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="mvp01e-fake",text="We can discuss that.")


@pytest.mark.parametrize("invalid",[
    {},
    {"name":"empty","steps":[]},
    {"name":"no-session","steps":[{"action":"message","text":"hello"}]},
    {"name":"duplicate","steps":[
        {"action":"session","name":"a"},{"action":"session","name":"a"},
        {"action":"message","text":"hello"},
    ]},
    {"name":"bad-mode","steps":[
        {"action":"session","name":"a"},
        {"action":"message","text":"hello","mode":"delete"},
    ]},
    {"name":"no-messages","steps":[{"action":"session","name":"a"}]},
    {"name":"forbidden","steps":[
        {"action":"session","name":"a"},
        {"action":"delete_database","name":"x"},
    ]},
])
def test_evaluation_scenario_rejects_unsafe_or_invalid_steps(invalid):
    with pytest.raises(ValueError):
        validate_scenario(invalid)



def test_ollama_interpreted_trace_equals_real_augmented_http_payload(monkeypatch):
    """The outbound JSON, not the upstream pre-interpreted messages, is evidence."""
    from persona_agent.conversation_contracts import ModelMessage
    from persona_agent.model_gateway import OllamaChatGateway

    captured=[]
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,n=-1):
            return BytesIO(json.dumps({
                "message":{"role":"assistant","content":json.dumps({
                    "reply":"That sounds achievable.","intent":None,
                })},
            }).encode()).read(n)

    def urlopen(request,timeout):
        captured.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr("persona_agent.model_gateway.urlopen",urlopen)
    observer=RecordingGateway(OllamaChatGateway(model="observed-model"))
    reply=observer.generate_interpreted([
        ModelMessage("system","Preserve selected persona."),
        ModelMessage("user","I've been thinking about walking more regularly."),
    ])
    assert reply.text=="That sounds achievable."
    assert len(captured)==len(observer.requests)==1
    trace=observer.requests[0]
    assert trace["capture_boundary"]=="ollama.outbound"
    assert trace["route"]=="generate_interpreted"
    assert trace["payload"]==captured[0]
    assert trace["payload"]["format"]=="json"
    assert trace["payload"]["model"]=="observed-model"
    assert trace["messages"]==captured[0]["messages"]
    assert len(trace["messages"])==3
    assert trace["messages"][0]["role"]=="system"
    assert "evidence_quote" in trace["messages"][0]["content"]
    assert "intent" in trace["messages"][0]["content"]
    assert trace["messages"][1]["content"]=="Preserve selected persona."
    assert trace["messages"][-1]["content"]==(
        "I've been thinking about walking more regularly."
    )


def test_outbound_observer_receives_detached_payload(monkeypatch):
    """An evaluator cannot edit the HTTP request by mutating its observation."""
    from persona_agent.conversation_contracts import ModelMessage
    from persona_agent.model_gateway import OllamaChatGateway

    seen=[]
    def mutate_copy(payload):
        payload["messages"][0]["content"]="INJECTED"
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,n=-1):
            return BytesIO(json.dumps({
                "message":{"role":"assistant","content":"Hello"}
            }).encode()).read(n)
    def urlopen(request,timeout):
        seen.append(json.loads(request.data))
        return FakeResponse()
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",urlopen)
    gateway=OllamaChatGateway(model="test",request_observer=mutate_copy)
    gateway.generate([ModelMessage("user","Hello")])
    assert seen[0]["messages"][0]["content"]=="Hello"



def test_interpreted_run_exports_actual_augmented_request_in_zip(
    dsn,tmp_path,monkeypatch,
):
    from persona_agent.model_gateway import OllamaChatGateway

    captured=[]
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,n=-1):
            return BytesIO(json.dumps({
                "message":{"role":"assistant","content":json.dumps({
                    "reply":"What schedule might suit you?","intent":None,
                })},
            }).encode()).read(n)
    def urlopen(request,timeout):
        captured.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr("persona_agent.model_gateway.urlopen",urlopen)
    scenario={"name":"interpreted-prompt-evidence","steps":[
        {"action":"session","name":"first"},
        {"action":"message",
         "text":"I've been thinking about walking more regularly."},
    ]}
    report=run_scenario(
        dsn,scenario,OllamaChatGateway(model="outbound-test"),
        run_memory_worker=False,
    )
    assert not report["errors"],report["errors"]
    assert len(report["model_requests"])==len(captured)==1
    sent=report["model_requests"][0]
    assert sent["capture_boundary"]=="ollama.outbound"
    assert sent["payload"]==captured[0]
    assert sent["payload"]["format"]=="json"
    assert "evidence_quote" in sent["messages"][0]["content"]
    path=write_bundle(
        report,scenario,tmp_path/"outbound.zip",
        model="outbound-test",git_sha="test",migration_hashes={},
    )
    with ZipFile(path) as z:
        export=json.loads(z.read("model_requests.json"))
    assert export==report["model_requests"]
    assert export[0]["payload"]==captured[0]


def test_evaluation_uses_real_host_and_captures_each_state_boundary(dsn,tmp_path):
    model=SimpleModel()
    scenario={
        "name":"mvp01e-test",
        "steps":[
            {"action":"session","name":"first"},
            {"action":"message","text":"Hello"},
            {"action":"message","text":"I want to walk more.",
             "expect_intent":"OBJECTIVE"},
            {"action":"restart"},
            {"action":"message","text":"What time is it in Berlin?",
             "expect_model_id":TIME_TOOL},
            {"action":"message","text":"Count words in: red blue blue",
             "expect_model_id":"tool.initial.deferred.v1"},
            {"action":"session","name":"later"},
            {"action":"message","text":"Please help with a new topic."},
        ],
    }
    result=run_scenario(dsn,scenario,model)
    assert not result["errors"],result["errors"]
    assert result["passed"],result["checks"]
    messages=[row for row in result["transcript"]
              if row["action"]=="message"]
    assert len(messages)==5
    assert len({row["conversation_id"] for row in messages})==2
    assert len(result["model_requests"])==3
    assert model.calls==3
    assert result["requires_human_review"]
    assert any(row["checkpoint"]=="after_worker_and_followup"
               for row in result["snapshots"])
    assert any(row["checkpoint"]=="after_explicit_memory_worker"
               for row in result["snapshots"])
    assert all(row["application"]["turn_trace"]["response_status"]=="DELIVERED"
               for row in result["snapshots"])
    assert all(row["application"]["counts"]["goals"]==0
               for row in result["snapshots"])
    assert messages[3]["followups_delivered"]==1
    assert result["deliveries"][-1]["type"]=="reply"

    path=write_bundle(
        result,scenario,tmp_path/"report.zip",
        model="test-model",git_sha="deadbeef",
        migration_hashes={"0001.sql":"abc"},
    )
    assert path.exists()
    with ZipFile(path) as z:
        assert set(z.namelist())=={
            "manifest.json","scenario.json","transcript.json",
            "state_snapshots.json","model_requests.json",
            "delivery_events.json","evaluation.json","errors.json",
        }
        manifest=json.loads(z.read("manifest.json"))
        assert manifest["contract_passed"]
        assert manifest["conversational_quality"]=="NOT_AUTOMATICALLY_GRADED"
        observed=json.loads(z.read("state_snapshots.json"))
        assert observed[0]["database"]["conversation_turns"]["count"]>=1


def test_scoped_snapshots_never_export_another_personas_records(dsn):
    first=seed_local_demo(dsn)
    other=seed_local_demo(dsn)
    gateway=SimpleModel()
    h1=BehavioralAcceptanceHarness(dsn,first,gateway)
    h2=BehavioralAcceptanceHarness(dsn,other,gateway)
    h1.begin_session("a")
    h2.begin_session("b")
    x=h1.send("I want to walk more.",idempotency_key="one")
    y=h2.send("I want to cook more.",idempotency_key="two")
    result=capture_state(
        dsn,h1,turn_id=x.turn_id,checkpoint="after_delivery",
    )
    all_rows=result["database"]["conversation_turns"]["rows"]
    assert [row["turn_id"] for row in all_rows]==[x.turn_id]
    assert y.turn_id not in json.dumps(result,default=str)
    assert result["database"]["events"]["count"]>=1


def test_partial_report_preserves_error_evidence(dsn,tmp_path):
    class FailingModel:
        def generate(self,messages):
            raise RuntimeError("synthetic model failure")
    scenario={"name":"failure","steps":[
        {"action":"session","name":"a"},
        {"action":"message","text":"Hello"},
    ]}
    result=run_scenario(dsn,scenario,FailingModel())
    assert not result["passed"]
    assert result["errors"][0]["type"]=="RuntimeError"
    failed=result["transcript"][-1]
    assert failed["action"]=="message_failed"
    assert failed["input"]=="Hello"
    assert failed["assistant"] is None
    assert failed["ingress_committed"] is True
    assert failed["turn_id"] is not None
    state=result["snapshots"][-1]
    assert state["checkpoint"]=="after_failed_turn"
    assert state["source_turn_id"]==failed["turn_id"]
    assert state["application"]["turn_trace"]["response_status"] is None
    assert any(str(row["turn_id"])==failed["turn_id"]
               for row in state["database"]["conversation_turns"]["rows"])
    assert state["database"]["conversation_responses"]["count"]==0
    assert state["database"]["events"]["count"]>=1
    saved=write_bundle(
        result,scenario,tmp_path/"failure.zip",
        model="test-model",git_sha=None,migration_hashes={},
    )
    assert saved.exists()
    with ZipFile(saved) as z:
        assert json.loads(z.read("errors.json"))[0]["type"]=="RuntimeError"
        assert json.loads(z.read("transcript.json"))[-1]["ingress_committed"]
        assert json.loads(z.read("state_snapshots.json"))[-1][
            "checkpoint"]=="after_failed_turn"


def test_isolated_database_lifecycle_refuses_arbitrary_drop(monkeypatch):
    """Database deletion must be limited to the exact freshly generated name."""
    import importlib.util
    from types import SimpleNamespace

    spec=importlib.util.spec_from_file_location(
        "persona_e2e_cli",Path(__file__).resolve().parents[1]
        /"scripts"/"persona_e2e.py",
    )
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    queries=[]
    class Connection:
        info=SimpleNamespace(dbname="postgres")
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def execute(self,statement):
            queries.append(statement)
    monkeypatch.setattr(module.psycopg,"connect",lambda *a,**kw:Connection())
    monkeypatch.setattr(
        module,"uuid4",lambda:__import__("uuid").UUID(int=1),
    )
    created,dsn=module.create_isolated_database("dbname=postgres user=local")
    assert created=="persona_e2e_0000000000000000"
    assert "persona_e2e_0000000000000000" in dsn
    with pytest.raises(ValueError,match="refusing to drop"):
        module.drop_isolated_database("dbname=postgres user=local","production")
    assert len(queries)==1
    module.drop_isolated_database("dbname=postgres user=local",created)
    assert len(queries)==2


def test_admin_dsn_must_target_postgres_maintenance_database(monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location(
        "persona_e2e_admin_guard",Path(__file__).resolve().parents[1]
        /"scripts"/"persona_e2e.py",
    )
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Connection:
        info=SimpleNamespace(dbname="production")
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def execute(self,*a):
            raise AssertionError("must not issue destructive SQL")
    monkeypatch.setattr(module.psycopg,"connect",lambda *a,**kw:Connection())
    with pytest.raises(ValueError,match="maintenance DB"):
        module.create_isolated_database("dbname=production user=local")

def test_failed_interpreted_turn_keeps_input_and_event_evidence(dsn):
    class FailingModel:
        def generate_interpreted(self,messages):
            raise RuntimeError("inference failed after durable ingress")
        def generate(self,messages):
            raise AssertionError("should use interpreted path")
    scenario={"name":"failure-with-intent","steps":[
        {"action":"session","name":"a"},
        {"action":"message",
         "text":"I've been thinking about walking more regularly."},
    ]}
    report=run_scenario(dsn,scenario,FailingModel(),run_memory_worker=False)
    assert report["errors"][0]["type"]=="RuntimeError"
    entry=report["transcript"][-1]
    assert entry["ingress_committed"] is True
    assert entry["turn_id"]
    state=report["snapshots"][-1]
    assert state["checkpoint"]=="after_failed_turn"
    assert state["application"]["turn_trace"]["turn_id"]==entry["turn_id"]
    assert state["database"]["conversation_turns"]["count"]==1
    assert state["database"]["events"]["count"]>=1
    assert state["database"]["conversation_responses"]["count"]==0


def test_failed_turn_observation_cannot_hide_original_exception(dsn,monkeypatch):
    class FailingModel:
        def generate(self,messages):
            raise RuntimeError("original generation failure")
    def cannot_observe(*args,**kwargs):
        raise ValueError("snapshot also failed")
    monkeypatch.setattr(
        "persona_agent.behavioral_evaluation.capture_state",cannot_observe,
    )
    scenario={"name":"double-failure","steps":[
        {"action":"session","name":"a"},
        {"action":"message","text":"I want to walk more."},
    ]}
    result=run_scenario(dsn,scenario,FailingModel(),run_memory_worker=False)
    assert not result["passed"]
    assert result["errors"][0]["type"]=="RuntimeError"
    assert "original generation failure" in result["errors"][0]["message"]
    assert result["errors"][1]["phase"]=="failure_observation"
    assert result["errors"][1]["type"]=="ValueError"
    assert result["transcript"][-1]["ingress_committed"] is True
    assert result["transcript"][-1]["turn_id"] is not None
    assert result["snapshots"]==[]
