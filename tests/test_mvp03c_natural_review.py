"""MVP-03C protocol validation, independent scoring and tamper resistance."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from persona_agent.natural_dialogue_review import (
    PROTOCOL,build_review_packet,packet_from_bundle,
    rating_template,summarize_independent_reviews,
    validate_packet,validate_ratings,
)
from persona_agent.scenario_validation import validate_scenario

SCENARIO=(
    Path(__file__).resolve().parents[1]
    /"scenarios"/"mvp03c_natural_dialogue.json"
)


def scenario():
    return json.loads(SCENARIO.read_text(encoding="utf-8"))


def synthetic_report():
    definition=validate_scenario(scenario())
    transcript=[]
    reviews=[]
    session=0
    for index,step in enumerate(definition["steps"]):
        if step["action"]=="session":
            session+=1
            transcript.append({
                "index":index,"action":"session",
                "name":step["name"],"conversation_id":f"session-{session}",
            })
        elif step["action"]=="restart":
            transcript.append({
                "index":index,"action":"restart",
                "conversation_id":f"session-{session}",
            })
        else:
            record={
                "index":index,"action":"message",
                "conversation_id":f"session-{session}",
                "input":step["text"],"assistant":"This is a synthetic test reply.",
                "turn_id":f"turn-{index}",
                "response_elapsed_ms":12.3,
            }
            transcript.append(record)
            if "review_response" in step:
                reviews.append({
                    "step_index":index,
                    "turn_id":f"turn-{index}",
                    "user":step["text"],
                    "assistant":record["assistant"],
                    "review_case":step["review_case"],
                })
    return {
        "name":definition["name"],"synthetic":True,
        "contract_passed":True,"errors":[],
        "transcript":transcript,"review_items":reviews,
    }


def completed(packet,reviewer):
    result=rating_template(packet,reviewer)
    for item in result["ratings"]:
        item["scores"]={key:2 for key in item["scores"]}
    return result


def test_published_protocol_has_three_sessions_real_restart_and_distinct_labels():
    definition=validate_scenario(scenario())
    assert len([x for x in definition["steps"]
                if x["action"]=="session"])==3
    assert len([x for x in definition["steps"]
                if x["action"]=="restart"])==2
    reviewed=[step for step in definition["steps"] if "review_case" in step]
    assert len(reviewed)>=10
    assert {"agency","affect","brevity","continuity","repetition"}<=set().union(
        *(set(row["review_case"]["dimensions"]) for row in reviewed)
    )
    assert all(step["mode"]=="model" for step in reviewed)


@pytest.mark.parametrize("change",[
    lambda steps: steps[2]["review_case"].update({"id":"bad id"}),
    lambda steps: steps[2]["review_case"].update({"dimensions":["unknown"]}),
    lambda steps: steps[2]["review_case"].update({"source_quotes":["not in history"]}),
    lambda steps: steps[2]["review_case"].update({"failure_criteria":[]}),
    lambda steps: steps[2].update({"mode":"auto"}),
    lambda steps: steps[2].pop("review_response"),
])
def test_invalid_or_untraceable_review_requirements_rejected(change):
    data=scenario()
    change(data["steps"])
    with pytest.raises(ValueError):
        validate_scenario(data)


def test_blind_packet_has_no_model_id_prompt_or_future_turns():
    report=synthetic_report()
    packet=build_review_packet(report,scenario())
    assert validate_packet(packet)==packet
    assert packet["protocol"]==PROTOCOL
    assert packet["ratings_status"]=="AWAITING_INDEPENDENT_HUMAN_REVIEW"
    assert len(packet["cases"])==len(report["review_items"])
    first=packet["cases"][0]
    assert first["response_elapsed_ms"]==12.3
    assert "I haven't chosen a schedule" in first["user"]
    assert all("astronomy" not in row["user"]
               for row in first["dialogue_history"])
    assert all("model_id" not in case and "model_requests" not in case
               and "system_prompt" not in case for case in packet["cases"])
    assert all(item["index"]<first["step_index"]
               for item in report["transcript"]
               if item["action"]=="message"
               and item["index"]<first["step_index"])
    tampered=deepcopy(packet)
    tampered["cases"][0]["assistant"]="Another reply"
    with pytest.raises(ValueError,match="integrity"):
        validate_packet(tampered)


def test_reviewer_requires_completed_independent_grades_and_concrete_negative_notes():
    packet=build_review_packet(synthetic_report(),scenario())
    alice=rating_template(packet,"reviewer_a")
    with pytest.raises(ValueError,match="0, 1 or 2"):
        validate_ratings(packet,alice)
    alice=completed(packet,"reviewer_a")
    assert validate_ratings(packet,alice)==alice
    alice["ratings"][0]["scores"]["calibration"]=1
    with pytest.raises(ValueError,match="concrete"):
        validate_ratings(packet,alice)
    alice["ratings"][0]["notes"]="Mistakes proposed cadence for a firm commitment."
    validate_ratings(packet,alice)
    wrong=deepcopy(alice)
    wrong["packet_sha256"]="0"*64
    with pytest.raises(ValueError,match="another evidence packet"):
        validate_ratings(packet,wrong)
    duplicate=deepcopy(alice)
    duplicate["ratings"].append(deepcopy(duplicate["ratings"][0]))
    with pytest.raises(ValueError,match="incomplete"):
        validate_ratings(packet,duplicate)


def test_two_reviewers_disagreement_and_critical_failures_require_adjudication():
    packet=build_review_packet(synthetic_report(),scenario())
    alice=completed(packet,"reviewer_a")
    bob=completed(packet,"reviewer_b")
    agreed=summarize_independent_reviews(packet,alice,bob)
    assert agreed["status"]=="INDEPENDENT_REVIEW_COMPLETE"
    assert agreed["exact_agreement_rate"]==1.0
    assert agreed["natural_quality_accepted"] is None
    first=bob["ratings"][0]
    first["scores"]["calibration"]=0
    first["notes"]="Claimed an agreed schedule although the source was tentative."
    first["critical_failures"]=["unjustified_commitment"]
    disagreement=summarize_independent_reviews(packet,alice,bob)
    assert disagreement["status"]=="REQUIRES_ADJUDICATION"
    assert disagreement["exact_agreement_rate"]<1.0
    assert disagreement["disagreements"][0]["case_id"]==first["case_id"]
    assert disagreement["critical_flags"][0]["code"]=="unjustified_commitment"
    with pytest.raises(ValueError,match="distinct"):
        summarize_independent_reviews(packet,alice,alice)


def test_packet_rejects_non_successful_or_mismatched_trace():
    observed=synthetic_report()
    observed["contract_passed"]=False
    with pytest.raises(ValueError,match="successful"):
        build_review_packet(observed,scenario())
    observed=synthetic_report()
    observed["review_items"][0]["assistant"]="Different output"
    with pytest.raises(ValueError,match="reconciled"):
        build_review_packet(observed,scenario())
    observed=synthetic_report()
    observed["review_items"][0]["review_case"]["dimensions"]=["grounding"]
    with pytest.raises(ValueError,match="rubric"):
        build_review_packet(observed,scenario())


def test_bundle_requires_synthetic_completed_host_trace(tmp_path):
    from zipfile import ZipFile
    packet=build_review_packet(synthetic_report(),scenario())
    report=synthetic_report()
    bundle=tmp_path/"bundle.zip"
    docs={
        "manifest.json":{"synthetic_data":True},
        "scenario.json":scenario(),
        "transcript.json":report["transcript"],
        "review_items.json":report["review_items"],
        "evaluation.json":{"contract_passed":True},
        "errors.json":[],
    }
    with ZipFile(bundle,"w") as archive:
        for name,value in docs.items():
            archive.writestr(name,json.dumps(value))
    assert packet_from_bundle(bundle)["packet_sha256"]==packet["packet_sha256"]
    docs["manifest.json"]["synthetic_data"]=False
    with ZipFile(bundle,"w") as archive:
        for name,value in docs.items():
            archive.writestr(name,json.dumps(value))
    with pytest.raises(ValueError,match="real-user"):
        packet_from_bundle(bundle)
