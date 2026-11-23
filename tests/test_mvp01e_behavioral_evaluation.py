"""MVP-01E: non-destructive scenario validation, evidence and real SQL tests."""
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
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
            "interpretation_diagnostics.json","semantic_diagnostics.json",
            "review_items.json","delivery_events.json","evaluation.json",
            "errors.json",
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


@pytest.mark.parametrize("with_semantic_flag", [False, True])
def test_compose_wrapper_checks_semantic_flags_before_startup(
    tmp_path, with_semantic_flag,
):
    """Use the REAL preflight; Docker is a stub that must not be called
    when the semantic scenario omits its mandatory flag."""
    root=Path(__file__).resolve().parents[1]
    scripts=tmp_path/"scripts"
    scripts.mkdir()
    shutil.copy2(
        root/"scripts"/"run_persona_e2e.sh",
        scripts/"run_persona_e2e.sh",
    )
    bin_dir=tmp_path/"bin"
    bin_dir.mkdir()
    docker=bin_dir/"docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "docker %s\\n" "$*" >> "$MOCK_LOG"\n'
        "exit 37\n",
    )
    docker.chmod(0o755)
    log=tmp_path/"docker-calls.log"
    env=os.environ.copy()
    env.update({
        "PATH":str(bin_dir)+os.pathsep+env.get("PATH",""),
        "MOCK_LOG":str(log),
        "E2E_POSTGRES_PASSWORD":"test-only",
        "PERSONA_E2E_ADMIN_DSN":"dbname=postgres user=test",
    })
    argv=[
        "bash",str(scripts/"run_persona_e2e.sh"),
        "--scenario",str(root/"scenarios"/"mvp02c3_semantic_diagnostics.json"),
    ]
    if with_semantic_flag:
        argv.append("--semantic-diagnostics")
    result=subprocess.run(
        argv,cwd=tmp_path,env=env,
        text=True,capture_output=True,timeout=20,
    )
    if with_semantic_flag:
        # Compatible invocation passes preflight and reaches fake Compose.
        # Docker exits 37 before any evidence marker can be published;
        # wrapper must normalize this uncertain infrastructure failure to 2.
        assert result.returncode==2, result.stderr
        assert "EVIDENCE NOT CONFIRMED" in result.stderr
        assert log.exists()
        assert " up -d --wait" in log.read_text()
    else:
        assert result.returncode==2, result.stderr
        assert "semantic expectations require --semantic-diagnostics" in result.stderr
        assert not log.exists(), "incompatible invocation started Compose"
        assert "EVIDENCE NOT CONFIRMED" not in result.stderr


@pytest.mark.parametrize("published,runner_exit,expect_teardown", [
    (False, 1, False),  # ZIP could not be written: preserve postgres volume
    (True, 1, True),    # Evaluated contract failure: valid ZIP, clean up
    (True, 0, True),    # Successful run: valid ZIP, clean up
    (False, 0, False), # Missing marker is unsafe even if runner exited zero
])
def test_compose_wrapper_preserves_evidence_on_failed_publication(
    tmp_path, published, runner_exit, expect_teardown,
):
    """No Docker/DB required: simulate the exact runner-to-wrapper contract."""
    root=Path(__file__).resolve().parents[1]
    scripts=tmp_path/"scripts"
    scripts.mkdir()
    shutil.copy2(root/"scripts"/"run_persona_e2e.sh",
                 scripts/"run_persona_e2e.sh")
    shims=tmp_path/"bin"
    shims.mkdir()
    docker=shims/"docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf 'docker %s\\n' \"$*\" >> \"$MOCK_LOG\"\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    python=shims/"python"
    python.write_text(
        "#!/bin/sh\n"
        "printf 'python %s\\n' \"$*\" >> \"$MOCK_LOG\"\n"
        "if [ \"$1\" = \"-\" ]; then\n"
        "  exit 0  # Scenario validation preflight is not the runner.\n"
        "fi\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"--publication-status-file\" ]; then\n"
        "    shift\n"
        "    if [ \"$MOCK_PUBLISHED\" = yes ]; then\n"
        "      printf 'PUBLISHED\\n' > \"$1\"\n"
        "    fi\n"
        "    break\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit \"$MOCK_RUNNER_EXIT\"\n"
    )
    python.chmod(0o755)
    log=tmp_path/"calls.log"
    env=os.environ.copy()
    env.update({
        "PATH":str(shims)+os.pathsep+env.get("PATH",""),
        "MOCK_LOG":str(log),
        "MOCK_PUBLISHED":"yes" if published else "no",
        "MOCK_RUNNER_EXIT":str(runner_exit),
        "E2E_POSTGRES_PASSWORD":"test-only",
        "PERSONA_E2E_ADMIN_DSN":"dbname=postgres user=test",
    })
    result=subprocess.run(
        ["bash",str(scripts/"run_persona_e2e.sh"),
         "--report","artifacts/new-report.zip"],
        cwd=tmp_path,env=env,text=True,capture_output=True,timeout=10,
    )
    # A published contract failure retains its runner status 1, whereas
    # missing publication always reports infrastructure failure 2.
    assert result.returncode == (runner_exit if published else 2)
    commands=log.read_text().splitlines()
    # Preflight is the first Python invocation; only the later runner call
    # simulates publication and returns MOCK_RUNNER_EXIT.
    assert commands[0].startswith("python - ")
    ups=[x for x in commands if x.startswith("docker ")
         and " up -d --wait" in x]
    runners=[x for x in commands
             if x.startswith("python scripts/persona_e2e.py ")
             and "--publication-status-file " in x]
    assert len(ups)==len(runners)==1
    assert commands.index(ups[0])<commands.index(runners[0])
    downs=[x for x in commands if " down -v " in x]
    assert bool(downs) is expect_teardown
    if not published:
        assert "EVIDENCE NOT CONFIRMED" in result.stderr
        match=re.search(r"preserving Compose project (persona-e2e-[a-zA-Z0-9-]+)",
                        result.stderr)
        assert match is not None
        assert match.group(1) in ups[0]
    else:
        assert "EVIDENCE NOT CONFIRMED" not in result.stderr


def test_runner_publishes_marker_only_after_zip_written(tmp_path,monkeypatch):
    """Publication failure skips both status signal and database teardown."""
    import importlib.util
    from contextlib import nullcontext

    spec=importlib.util.spec_from_file_location(
        "persona_e2e_publish_contract",
        Path(__file__).resolve().parents[1]/"scripts"/"persona_e2e.py",
    )
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    migrations=tmp_path/"migrations"
    migrations.mkdir()
    (migrations/"0001.sql").write_text("SELECT 1;")
    scenario=tmp_path/"scenario.json"
    scenario.write_text(json.dumps({"name":"sample","steps":[
        {"action":"session","name":"one"},
        {"action":"message","text":"Hello"},
    ]}))
    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def transaction(self):
            return nullcontext()
        def execute(self,*args):
            pass
    monkeypatch.setattr(module.psycopg,"connect",lambda *args,**kwargs:FakeConnection())
    monkeypatch.setattr(module,"create_isolated_database",
                        lambda _:("persona_e2e_0000000000000000","synthetic-dsn"))
    dropped=[]
    monkeypatch.setattr(module,"drop_isolated_database",
                        lambda *_:dropped.append(True))
    monkeypatch.setattr(module,"OllamaChatGateway",lambda **kw: object())
    monkeypatch.setattr(module,"git_revision",lambda:"local-test")
    monkeypatch.setattr(module,"run_scenario",lambda *args,**kwargs:{
        "format_version":"mvp01e.1","name":"sample",
        "started_at":"now","completed_at":"now","passed":True,
        "transcript":[],"snapshots":[],"checks":[],
        "model_requests":[],"deliveries":[],"errors":[],
    })
    report=tmp_path/"review.zip"
    marker=tmp_path/"publication.status"
    argv=[
        "--admin-dsn","dbname=postgres","--scenario",str(scenario),
        "--report",str(report),"--publication-status-file",str(marker),
    ]
    def fail_publish(*args,**kwargs):
        raise OSError("simulated full disk")
    monkeypatch.setattr(module,"write_bundle",fail_publish)
    with pytest.raises(OSError,match="full disk"):
        module.run(argv)
    assert not marker.exists()
    assert not dropped

    # A published ZIP with unsuccessful behavioral checks is still safe to
    # clean up: publication status, rather than exit code, is the contract.
    def successful_publish(report_obj,scenario_obj,destination,**kw):
        destination.write_bytes(b"valid-test-bundle")
    monkeypatch.setattr(module,"write_bundle",successful_publish)
    assert module.run(argv)==0
    assert report.exists()
    assert marker.read_text()=="PUBLISHED\n"
    assert dropped==[True]

    # A valid ZIP without a publish marker is an uncertain state: retain DB.
    marker.unlink()
    report.unlink()  # The CLI intentionally refuses to overwrite ZIP reports.
    dropped.clear()
    def fail_marker(path):
        raise OSError("status directory unwritable")
    monkeypatch.setattr(module,"publish_status_file",fail_marker)
    with pytest.raises(OSError,match="status directory"):
        module.run(argv)
    assert report.exists()
    assert not marker.exists()
    assert not dropped


def test_e2e_processes_relationship_preference_before_later_session_prompts(dsn):
    """Use real P0 event deliveries; no fixture writes into Relationship."""
    from persona_agent.relationship_state import read_relationship_state
    from persona_agent.contracts import TrustedActor

    class CapturingModel:
        def __init__(self):
            self.prompts = []
        def generate(self, messages):
            self.prompts.append(tuple(messages))
            return ModelReply(model_id="e2e-preference.capture",
                              text="We can continue.")

    instruction = (
        "Please keep replies to one or two sentences "
        "unless I ask for detailed instructions."
    )
    scenario = {"name":"e2e-affect-preference", "steps":[
        {"action":"session","name":"first"},
        {"action":"message","text":instruction,"mode":"model"},
        {"action":"restart"},
        {"action":"session","name":"later"},
        {"action":"message","text":"Tell me a fun fact.","mode":"model"},
        {"action":"message","text":"Please use normal length replies.","mode":"model"},
        {"action":"session","name":"after_correction"},
        {"action":"message","text":"Tell me another fact.","mode":"model"},
    ]}
    model = CapturingModel()
    result = run_scenario(dsn,scenario,model,run_memory_worker=False)
    assert result["passed"],(result["errors"],result["checks"])
    assert len(model.prompts)==4

    first = next(x for x in result["transcript"]
                 if x.get("input")==instruction)
    preference_snapshot = next(
        x for x in result["snapshots"]
        if x["checkpoint"]=="after_affect_worker"
        and x["source_turn_id"]==first["turn_id"]
    )
    preferences=preference_snapshot["database"]["relationship_preferences"]["rows"]
    assert len(preferences)==1
    assert preferences[0]["preference_key"]=="RESPONSE_LENGTH"
    assert preferences[0]["preference_value"]=="SHORT"
    assert preferences[0]["source_quote"]==instruction
    assert preferences[0]["source_turn_id"]==first["turn_id"]
    assert preference_snapshot["database"]["relationship_observations"]["count"]==1
    assert next(x for x in result["transcript"] if x.get("input")==instruction)[
        "affect_worker"]["relationship_recorded"]>=1

    later_prompt = model.prompts[1][0].content
    assert "keep replies to one or two sentences" in later_prompt
    assert "unless the current request asks for detailed instructions" in later_prompt
    assert "Follow a direct request for more detail" in later_prompt

    corrected_prompt = model.prompts[-1][0].content
    assert "Explicit user preference: response length normal." in corrected_prompt
    assert "keep replies to one or two sentences" not in corrected_prompt
    assert result["snapshots"][-1]["database"]["relationship_preferences"]["count"]==1

    user_id=result["identity"]["user_id"]
    instance_id=result["identity"]["instance_id"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        state=read_relationship_state(
            conn,TrustedActor(user_id=__import__("uuid").UUID(user_id),
                              source_id="e2e-relationship-read"),
            __import__("uuid").UUID(instance_id),
        )
    matching=[p for p in state.preferences if p.preference_key=="RESPONSE_LENGTH"]
    assert len(matching)==1
    assert matching[0].preference_value=="NORMAL"
    assert matching[0].version==2


def test_e2e_affect_worker_can_be_disabled_explicitly(dsn):
    model=SimpleModel()
    scenario={"name":"without-affect","steps":[
        {"action":"session","name":"first"},
        {"action":"message","mode":"model",
         "text":"Please keep replies to one or two sentences."},
    ]}
    result=run_scenario(dsn,scenario,model,
                        run_memory_worker=False,run_affect_worker=False)
    assert result["passed"],result["errors"]
    assert not any(x["checkpoint"]=="after_affect_worker"
                   for x in result["snapshots"])
    assert result["snapshots"][-1]["database"]["relationship_preferences"]["count"]==0
