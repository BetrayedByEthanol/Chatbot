"""MVP-01F.1: observational model-intent evidence and acceptance gates."""
import json
from pathlib import Path
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest

from persona_agent.behavioral_diagnostics import describe_interpretation
from persona_agent.behavioral_evaluation import (
    _check_turn, run_scenario, validate_scenario, write_bundle,
)
from persona_agent.conversation_contracts import ModelIntentCandidate, ModelReply
from persona_agent.intent_interpretation import diagnose_model_candidate_gate
from persona_agent.model_gateway import OllamaChatGateway


WALKING="I've been thinking about walking more regularly."


def test_candidate_diagnostics_identify_model_vs_schema_vs_admission():
    assert describe_interpretation('{"reply":"Sure","intent":null}')[
        "outcome"]=="INTENT_NULL"
    malformed=describe_interpretation(json.dumps({
        "reply":"Sure","intent":{"kind":"NOT_A_KIND","confidence":0.99}
    }))
    assert malformed["outcome"]=="INTENT_SCHEMA_REJECTED"
    assert malformed["validation_errors"]
    assert describe_interpretation("ordinary response")[
        "outcome"]=="PLAIN_RESPONSE_FALLBACK"
    assert describe_interpretation(None)["outcome"]=="RAW_RESPONSE_UNAVAILABLE"

    candidate=ModelIntentCandidate(
        kind="OBJECTIVE",evidence_quote=WALKING,
        confidence=0.9,reference="NONE",
    )
    event=uuid4()
    assert diagnose_model_candidate_gate(WALKING,event,candidate)=="VALID"
    assert diagnose_model_candidate_gate(WALKING,None,candidate)=="SOURCE_EVENT_MISSING"
    assert diagnose_model_candidate_gate(
        "Hello!",event,candidate,
    )=="INPUT_NOT_ELIGIBLE"
    assert diagnose_model_candidate_gate(
        WALKING,event,candidate.model_copy(update={"confidence":0.4}),
    )=="BELOW_CONFIDENCE_THRESHOLD"
    assert diagnose_model_candidate_gate(
        WALKING,event,candidate.model_copy(update={"kind":"INSTRUCTION"}),
    )=="MODEL_INSTRUCTION_DISALLOWED"
    assert diagnose_model_candidate_gate(
        WALKING,event,candidate.model_copy(update={"evidence_quote":"jogging"}),
    )=="QUOTE_NOT_GROUNDED"


@pytest.mark.parametrize("invalid",[
    {"expect_related_intent":"DELETE"},
    {"expect_focus_contains":""},
    {"expect_focus_contains":24},
    {"expect_no_new_operations":"true"},
    {"review_response":""},
])
def test_reject_invalid_behavior_expectations(invalid):
    scenario={"name":"bad-annotations","steps":[
        {"action":"session","name":"first"},
        {"action":"message","text":"Hello",**invalid},
    ]}
    with pytest.raises(ValueError):
        validate_scenario(scenario)


def test_focus_reference_and_tool_assertions_read_only():
    turn=str(uuid4())
    objective=str(uuid4())
    correction=str(uuid4())
    thread=str(uuid4())
    snapshot={
        "application":{
            "turn_trace":{"response_status":"DELIVERED"},
            "counts":{"goals":0},
            "dialogue":{
                "focus_thread_id":thread,
                "threads":[{"thread_id":thread,"title":"Walking twice a week"}],
            },
        },
        "database":{
            "conversation_intent_signals":{"rows":[
                {"turn_id":"earlier","signal_id":objective,
                 "intent_kind":"OBJECTIVE","related_signal_id":None},
                {"turn_id":turn,"signal_id":correction,
                 "intent_kind":"CORRECTION","related_signal_id":objective},
            ]},
            "operations":{"count":2},
        },
    }
    step={
        "expect_intent":"CORRECTION",
        "expect_related_intent":"OBJECTIVE",
        "expect_focus_contains":"walk",
        "expect_no_new_operations":True,
    }
    checks=_check_turn(step,{"turn_id":turn,"model_id":"stub"},snapshot,
                       previous_operations=2)
    assert all(x["pass"] for x in checks),checks
    assert all(x["category"]=="behavioral" for x in checks
               if x["check"] not in ("response_delivered","no_unapproved_goal"))
    assert not any(x["pass"] for x in _check_turn(
        step,{"turn_id":turn,"model_id":"stub"},snapshot,previous_operations=1,
    ) if x["check"]=="no_new_operations")
    assert not any(x["pass"] for x in _check_turn(
        {**step,"expect_focus_contains":"cooking"},
        {"turn_id":turn,"model_id":"stub"},snapshot,previous_operations=2,
    ) if x["check"]=="focused_topic")


def _http_reply(monkeypatch, intent):
    """Intercept real Ollama JSON protocol while preserving gateway logic."""
    posted=[]
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self,n=-1):
            return BytesIO(json.dumps({
                "message":{"role":"assistant","content":json.dumps({
                    "reply":"Walking is worth exploring.","intent":intent,
                })},
            }).encode()).read(n)
    def urlopen(request,timeout):
        posted.append(json.loads(request.data))
        return FakeResponse()
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",urlopen)
    return posted


@pytest.mark.parametrize("intent,expected_outcome,gate_reason,signal_expected",[
    (None,"INTENT_NULL",None,False),
    ({"kind":"OBJECTIVE","evidence_quote":"not from the user",
      "confidence":0.94,"reference":"NONE"},
     "VALID_CANDIDATE","QUOTE_NOT_GROUNDED",False),
    ({"kind":"OBJECTIVE","evidence_quote":WALKING,
      "confidence":0.94,"reference":"NONE"},
     "VALID_CANDIDATE","VALID",True),
])
def test_real_gateway_interpretation_evidence_and_acceptance(
    dsn,monkeypatch,tmp_path,intent,expected_outcome,gate_reason,signal_expected,
):
    calls=_http_reply(monkeypatch,intent)
    scenario={"name":"walking-intent","steps":[
        {"action":"session","name":"first"},
        {"action":"message","text":WALKING,"expect_intent":"OBJECTIVE",
         "expect_focus_contains":"walk",
         "review_response":"Is this a tentative objective, not an approved goal?"},
    ]}
    report=run_scenario(
        dsn,scenario,OllamaChatGateway(model="synthetic-ollama"),
        run_memory_worker=False,
    )
    assert not report["errors"],report["errors"]
    assert report["contract_passed"] is True
    assert report["behavioral_checks_passed"] is signal_expected
    assert report["passed"] is signal_expected
    assert report["behavioral_accepted"] is None
    assert report["requires_human_review"]
    assert len(report["review_items"])==1
    assert report["review_items"][0]["status"]=="PENDING_HUMAN_REVIEW"
    assert len(report["interpretation_diagnostics"])==1
    diagnostic=report["interpretation_diagnostics"][0]
    assert diagnostic["outcome"]==expected_outcome
    assert diagnostic["turn_id"]==report["transcript"][1]["turn_id"]
    assert diagnostic["step_index"]==1
    assert diagnostic["admission"]==(
        "MODEL_SIGNAL_PERSISTED" if signal_expected else
        "NO_VALID_MODEL_CANDIDATE" if intent is None else "GATE_REJECTED"
    )
    if gate_reason:
        assert diagnostic["gate_reason"]==gate_reason
    assert len(calls)==1
    assert calls[0]["format"]=="json"
    assert len(report["model_requests"])==1
    bundle=write_bundle(
        report,scenario,tmp_path/"acceptance.zip",
        model="synthetic-ollama",git_sha="test",migration_hashes={},
    )
    with ZipFile(bundle) as archive:
        evidence=json.loads(archive.read("interpretation_diagnostics.json"))
        review=json.loads(archive.read("review_items.json"))
        evaluation=json.loads(archive.read("evaluation.json"))
        manifest=json.loads(archive.read("manifest.json"))
    assert evidence==report["interpretation_diagnostics"]
    assert review==report["review_items"]
    assert evaluation["contract_passed"] is True
    assert evaluation["behavioral_checks_passed"] is signal_expected
    assert evaluation["behavioral_accepted"] is None
    assert manifest["contract_passed"] is True


def test_missing_model_intent_is_a_behavioral_failure_not_a_contract_failure(dsn):
    class NoIntentModel:
        def generate_interpreted(self,messages):
            return ModelReply("stub","We could walk more.",intent_candidate=None)
        def generate(self,messages):
            raise AssertionError("implicit message should use interpreted route")
    scenario={"name":"missing-objective","steps":[
        {"action":"session","name":"first"},
        {"action":"message","text":WALKING,"expect_intent":"OBJECTIVE"},
    ]}
    result=run_scenario(dsn,scenario,NoIntentModel(),run_memory_worker=False)
    assert not result["errors"]
    assert result["contract_passed"] is True
    assert result["behavioral_checks_passed"] is False
    assert result["passed"] is False
    assert any(x["check"]=="intent" and not x["pass"] for x in result["checks"])
    assert result["interpretation_diagnostics"][0]["outcome"]=="RAW_RESPONSE_UNAVAILABLE"


def test_default_scenario_marks_known_behavioral_gaps_without_forcing_mvp02():
    scenario=json.loads((
        Path(__file__).resolve().parents[1]/
        "scenarios"/"mvp01e_conversation.json"
    ).read_text(encoding="utf-8"))
    validate_scenario(scenario)
    messages={step["text"]:step for step in scenario["steps"]
              if step["action"]=="message"}
    assert len(messages)==28
    assert messages[WALKING]["expect_intent"]=="OBJECTIVE"
    assert "walk" in messages[WALKING]["expect_focus_contains"]
    correction=messages["Actually, twice per week would be better."]
    assert correction["expect_intent"]=="CORRECTION"
    assert correction["expect_related_intent"]=="OBJECTIVE"
    assert messages["Switch the topic to cooking."][
        "expect_focus_contains"]=="cook"
    assert messages["What about that walking goal?"][
        "expect_focus_contains"]=="walk"
    assert messages["Can you run this for me?"][
        "expect_no_new_operations"] is True
    assert messages["What if I asked you to delete my data?"][
        "expect_no_new_operations"] is True
    # No invented cross-session memory assertions are treated as a
    # deterministic MVP-01 contract. MVP-02 must supply recall later.
    memory_question=messages[
        "Do you remember what we discussed about walking?"
    ]
    assert "review_response" in memory_question
    assert "expect_intent" not in memory_question
    assert "expect_focus_contains" not in memory_question
