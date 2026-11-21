"""MVP-03C.1: target summary must be real-model evidence, not human scores.

Synthetic fake replies are used ONLY to test the offline aggregation protocol.
They do not constitute target-model or human rating observations.
"""
from __future__ import annotations

from copy import deepcopy
import json
from zipfile import ZipFile

import pytest

from persona_agent.target_model_benchmark import (
    REPORT_FORMAT,summarize_target_benchmark,
)
from persona_agent.intent_interpretation import should_interpret_with_model
from test_mvp03c_natural_review import synthetic_report,scenario

MODEL="qwen2.5:7b"
DIGEST="sha256:"+"a"*64


def bundle(tmp_path,*,name="trace.zip",model=MODEL,modify=None):
    report=synthetic_report()
    for event in report["transcript"]:
        if event["action"]=="message":
            event.update({
                "model_id":model,"replayed":False,
                "response_elapsed_ms":12.5,
            })
    # Simulated, fully correlated host evidence. The test fixture is not
    # proof that a real Ollama model ran; production ZIPs come from the
    # existing authenticated E2E host and its independent observers.
    requests=[]
    deliveries=[]
    snapshots=[]
    checks=[]
    definition=scenario()
    for event in report["transcript"]:
        if event["action"]!="message":
            continue
        event["response_id"]=f"synthetic-response-{event['index']}"
        request_messages=[
            {"role":"system","content":"Test instruction"},
            {"role":"user","content":event["input"]},
        ]
        interpreted=should_interpret_with_model(event["input"])
        requests.append({
            "capture_boundary":"ollama.outbound",
            "route":"generate_interpreted" if interpreted else "generate",
            "payload":{
                "model":model,"stream":False,
                "messages":request_messages,
                **({"format":"json"} if interpreted else {}),
            },
            "messages":request_messages,
        })
        deliveries.append({
            "type":"reply","turn_id":event["turn_id"],
            "response_id":event["response_id"],
            "model_id":model,"text":event["assistant"],"replayed":False,
        })
        snapshots.append({
            "step_index":event["index"],"checkpoint":"after_delivery",
            "source_turn_id":event["turn_id"],
            "application":{"turn_trace":{
                "turn_id":event["turn_id"],
                "conversation_id":event["conversation_id"],
                "response_id":event["response_id"],
                "model_id":model,"response_status":"DELIVERED",
            }},
            "database":{
                "conversation_turns":{
                    "truncated":False,
                    "rows":[{
                        "turn_id":event["turn_id"],
                        "conversation_id":event["conversation_id"],
                        "input_text":event["input"],
                    }],
                },
                "conversation_responses":{
                    "truncated":False,
                    "rows":[{
                        "response_id":event["response_id"],
                        "turn_id":event["turn_id"],
                        "response_text":event["assistant"],
                        "model_id":model,"delivery_status":"DELIVERED",
                    }],
                },
            },
        })
        for kind in ("response_delivered","no_unapproved_goal"):
            checks.append({
                "step_index":event["index"],"check":kind,"pass":True,
            })
        if definition["steps"][event["index"]].get("expect_no_new_operations"):
            checks.append({
                "step_index":event["index"],
                "check":"no_new_operations","pass":True,
            })
    docs={
        "model_requests.json":requests,
        "delivery_events.json":deliveries,
        "state_snapshots.json":snapshots,
        "manifest.json":{
            "synthetic_data":True,"model":model,
            "git_sha":"example-commit","started_at":"2026-11-21T10:00:00Z",
            "finished_at":"2026-11-21T10:05:00Z",
            "migration_sha256":{"0001.sql":"example-sha"},
            "contract_passed":True,
            "behavioral_accepted":None,
        },
        "scenario.json":scenario(),
        "transcript.json":report["transcript"],
        "review_items.json":report["review_items"],
        "evaluation.json":{
            "contract_passed":True,"behavioral_checks_passed":True,
            "behavioral_accepted":None,"checks":checks,
        },
        "errors.json":[],
    }
    if modify is not None:
        modify(docs)
    path=tmp_path/name
    with ZipFile(path,"w") as file:
        for member,content in docs.items():
            file.writestr(member,json.dumps(content))
    return path


def test_valid_synthetic_bundle_is_source_free_and_pending_real_humans(tmp_path):
    path=bundle(tmp_path)
    result=summarize_target_benchmark(
        path,scenario(),model=MODEL,model_digest=DIGEST,
    )
    assert result["format_version"]==REPORT_FORMAT
    assert result["evidence_status"]=="COMPLETE_SYNTHETIC_TARGET_RUN"
    assert result["natural_quality_status"]=="PENDING_INDEPENDENT_HUMAN_RATINGS"
    assert result["natural_quality_accepted"] is None
    assert result["rating_data_present"] is False
    assert result["target_digest"]==DIGEST
    assert result["message_count"]==18
    assert result["review_case_count"]==15
    assert result["host_response_seconds"]["p95_nearest_rank"]==0.01
    assert result["host_response_seconds"]["median"]==0.01
    assert result["response_word_counts"]["median"]==6
    assert result["responses_over_80_words"]==0
    assert result["review_dimensions"]["continuity"]>0
    assert len(result["scenario_sha256"])==64
    assert len(result["evidence_zip_sha256"])==64
    output=json.dumps(result)
    assert "synthetic test reply" not in output
    assert "thinking about walking" not in output
    assert "reviewer_a" not in output
    assert "model_requests" not in output
    assert "dialogue_history" not in output


def test_benchmark_matches_real_primary_gateway_route_per_turn(tmp_path):
    """Distinguish plain generation from structured cue inference.

    The scenario uses the real deterministic selector. A fabricated
    structured envelope for an ordinary user message must be rejected.
    """
    sample=scenario()
    assert all(
        step.get("mode")=="model" for step in sample["steps"]
        if step["action"]=="message"
    )
    assert should_interpret_with_model(
        "Hello, I might start exercising."
    ) in (True,False)
    report=summarize_target_benchmark(
        bundle(tmp_path),sample,model=MODEL,
    )
    assert report["message_count"]==18


def test_wrong_gateway_route_or_json_format_is_rejected(tmp_path):
    sample=bundle(tmp_path,modify=lambda d:(
        d["model_requests.json"][0].update(route="generate_interpreted")
        if d["model_requests.json"][0]["route"]=="generate"
        else d["model_requests.json"][0].update(route="generate")
    ))
    with pytest.raises(ValueError,match="unverified target gateway request"):
        summarize_target_benchmark(sample,scenario(),model=MODEL)
    second=bundle(
        tmp_path,name="format.zip",modify=lambda d:
            d["model_requests.json"][0]["payload"].update(format="json")
            if d["model_requests.json"][0]["route"]=="generate"
            else d["model_requests.json"][0]["payload"].pop("format"),
    )
    with pytest.raises(ValueError,match="outbound target-model request mismatch"):
        summarize_target_benchmark(second,scenario(),model=MODEL)


def test_wrong_model_and_scenario_refuse_to_measure(tmp_path):
    path=bundle(tmp_path)
    with pytest.raises(ValueError,match="declared target"):
        summarize_target_benchmark(path,scenario(),model="qwen2.5:3b")
    wrong=deepcopy(scenario())
    wrong["description"]="A changed scenario label."
    with pytest.raises(ValueError,match="scenario or evidence identity mismatch"):
        summarize_target_benchmark(path,wrong,model=MODEL)


@pytest.mark.parametrize("mutator,reason",[
    (lambda d:d["manifest.json"].update(synthetic_data=False),"identity mismatch"),
    (lambda d:d["manifest.json"].update(contract_passed=False),"contracts failed"),
    (lambda d:d["evaluation.json"].update(behavioral_checks_passed=False),"contracts failed"),
    (lambda d:d["errors.json"].append({"type":"ModelUnavailable"}),"contracts failed"),
    (lambda d:d["evaluation.json"].update(behavioral_accepted=True),"pre-approved"),
    (lambda d:d["transcript.json"][2].update(model_id="mvp03c-protocol-fake"),"non-target"),
    (lambda d:d["transcript.json"][2].update(replayed=True),"replayed"),
    (lambda d:d["transcript.json"][2].update(response_elapsed_ms=-2),"invalid host"),
    (lambda d:d["transcript.json"][2].update(response_elapsed_ms=float("nan")),"invalid host"),
    (lambda d:d["transcript.json"][1].update(input="fabricated source"),"outbound target-model request mismatch"),
])
def test_incomplete_or_fabricated_evidence_is_not_scored(tmp_path,mutator,reason):
    path=bundle(tmp_path,modify=mutator)
    with pytest.raises(ValueError,match=reason):
        summarize_target_benchmark(path,scenario(),model=MODEL)


@pytest.mark.parametrize("digest",["bad","sha256:short","sha256:"+"G"*64])
def test_rejects_invalid_target_digests(tmp_path,digest):
    with pytest.raises(ValueError,match="model digest"):
        summarize_target_benchmark(
            bundle(tmp_path),scenario(),model=MODEL,model_digest=digest,
        )


def test_unprefixed_real_ollama_tags_digest_is_normalized(tmp_path):
    result=summarize_target_benchmark(
        bundle(tmp_path),scenario(),model=MODEL,
        model_digest="a"*64,
    )
    assert result["target_digest"]==DIGEST
    assert result["model_digest_recorded"] is True


def test_cli_reads_documented_ollama_tags_digest(tmp_path):
    import runpy
    from pathlib import Path
    tags=tmp_path/"ollama-tags.json"
    tags.write_text(json.dumps({"models":[{
        "name":MODEL,"model":MODEL,
        "digest":"a"*64,"size":4683075271,
        "details":{"parameter_size":"7.6B","quantization_level":"Q4_K_M"},
    }]}),encoding="utf-8")
    script=runpy.run_path(str(
        Path(__file__).resolve().parents[1]
        /"scripts"/"benchmark_target_model.py"
    ))
    observed_digest=script["_ollama_digest"](tags,MODEL)
    assert observed_digest=="a"*64
    summary=summarize_target_benchmark(
        bundle(tmp_path),scenario(),
        model=MODEL,model_digest=observed_digest,
    )
    assert summary["target_digest"]==DIGEST


def test_missing_digest_disclosed_not_faked(tmp_path):
    result=summarize_target_benchmark(bundle(tmp_path),scenario(),model=MODEL)
    assert result["target_digest"] is None
    assert result["model_digest_recorded"] is False


@pytest.mark.parametrize("mutator,reason",[
    (lambda d:d.pop("model_requests.json"),"unsafe or incomplete"),
    (lambda d:d["model_requests.json"][0].update(
        capture_boundary="pre_gateway_unverified"),"unverified target"),
    (lambda d:d["model_requests.json"][0]["payload"].update(
        model="mvp03c-fake"),"outbound target"),
    (lambda d:d["delivery_events.json"][0].update(
        text="A forged delivery"),"observed reply delivery"),
    (lambda d:d["state_snapshots.json"][0]["application"]["turn_trace"].update(
        response_status="GENERATED"),"durable turn trace"),
    (lambda d:d["state_snapshots.json"][0]["database"]
            ["conversation_responses"]["rows"][0].update(
                response_text="Forged SQL output"),"database turns"),
    (lambda d:d["evaluation.json"].update(checks=[]),"missing required"),
])
def test_missing_outbound_delivery_and_sql_evidence_rejected(
    tmp_path,mutator,reason
):
    path=bundle(tmp_path,modify=mutator)
    with pytest.raises(ValueError,match=reason):
        summarize_target_benchmark(path,scenario(),model=MODEL)


def test_duplicate_zip_members_rejected(tmp_path):
    path=bundle(tmp_path)
    with ZipFile(path,"a") as z:
        z.writestr("manifest.json","{}")
    with pytest.raises(ValueError,match="unsafe or incomplete"):
        summarize_target_benchmark(path,scenario(),model=MODEL)
