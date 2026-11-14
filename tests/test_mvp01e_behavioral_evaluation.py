"""MVP-01E: non-destructive scenario validation, evidence and real SQL tests."""
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
    saved=write_bundle(
        result,scenario,tmp_path/"failure.zip",
        model="test-model",git_sha=None,migration_hashes={},
    )
    assert saved.exists()
    with ZipFile(saved) as z:
        assert json.loads(z.read("errors.json"))[0]["type"]=="RuntimeError"
