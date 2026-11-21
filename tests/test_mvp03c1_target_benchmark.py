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
    docs={
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
            "behavioral_accepted":None,
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
    (lambda d:d["transcript.json"][1].update(input="fabricated source"),"trace mismatch"),
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


def test_missing_digest_disclosed_not_faked(tmp_path):
    result=summarize_target_benchmark(bundle(tmp_path),scenario(),model=MODEL)
    assert result["target_digest"] is None
    assert result["model_digest_recorded"] is False


def test_duplicate_zip_members_rejected(tmp_path):
    path=bundle(tmp_path)
    with ZipFile(path,"a") as z:
        z.writestr("manifest.json","{}")
    with pytest.raises(ValueError,match="unsafe or incomplete"):
        summarize_target_benchmark(path,scenario(),model=MODEL)
