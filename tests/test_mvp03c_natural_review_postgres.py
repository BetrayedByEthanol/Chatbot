"""MVP-03C real PostgreSQL acceptance of reviewable natural conversation output.

A fake model proves the review *pipeline* only. It is deliberately not
treated as evidence of natural language quality. Actual human judgments
must follow a target-model E2E run through the unchanged host.
"""
import json
from pathlib import Path

import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.behavioral_evaluation import (
    run_scenario,write_bundle,
)
from persona_agent.conversation_contracts import ModelReply
from persona_agent.natural_dialogue_review import (
    build_review_packet,packet_from_bundle,
    summarize_independent_reviews,rating_template,
)

SCENARIO=Path(__file__).resolve().parents[1]/"scenarios"/"mvp03c_natural_dialogue.json"


class RecordedFake:
    def __init__(self):
        self.messages=[]
    def generate(self,messages):
        self.messages.append(tuple(messages))
        return ModelReply(
            model_id="mvp03c-protocol-fake",
            text="I can discuss that, though I cannot verify real-world actions.",
        )


def test_actual_host_delivers_reviewable_three_session_trace(dsn,tmp_path):
    scenario=json.loads(SCENARIO.read_text(encoding="utf-8"))
    model=RecordedFake()
    report=run_scenario(
        dsn,scenario,model,
        run_tool_worker=False,run_memory_worker=False,
    )
    assert not report["errors"],report["errors"]
    assert report["contract_passed"],report["checks"]
    observed=[e for e in report["transcript"] if e["action"]=="message"]
    assert len(observed)==18
    assert len({item["conversation_id"] for item in observed})==3
    assert len(model.messages)==len(observed)
    assert all(item["response_elapsed_ms"]>=0 for item in observed)
    assert all(item["model_id"]=="mvp03c-protocol-fake" for item in observed)
    assert report["requires_human_review"]
    assert report["behavioral_accepted"] is None

    packet=build_review_packet(report,scenario)
    assert len(packet["cases"])==15
    assert {x["session"] for x in packet["cases"]}=={
        "tentative_walking_and_support","unrelated_break","return_and_correction",
    }
    assert all(case["response_words"]>0 for case in packet["cases"])
    assert all(
        "mvp03c-protocol-fake" not in json.dumps(case)
        for case in packet["cases"]
    )
    assert report["checks"] and all(
        item["pass"] for item in report["checks"]
    ),report["checks"]

    path=write_bundle(
        report,scenario,tmp_path/"mvp03c-evidence.zip",
        model="mvp03c-protocol-fake",git_sha="e2e-fixture",
        migration_hashes={"test.sql":"hash"},
    )
    from_bundle=packet_from_bundle(path)
    assert from_bundle["packet_sha256"]==packet["packet_sha256"]
    assert from_bundle["ratings_status"]=="AWAITING_INDEPENDENT_HUMAN_REVIEW"
    # The fake's artificial rating cannot constitute a human review.
    template=rating_template(from_bundle,"human_reviewer_a")
    assert all(value is None for item in template["ratings"]
               for value in item["scores"].values())
