"""MVP-02A: behavioral recall baseline through the real conversation host.

The former strict XFAIL is now an enforced PASS contract: MVP-02B must
surface an authenticated prior-session source-turn citation to the model.
Do not satisfy it by seeding Memory/Goals or injecting fake model context.
"""
import json
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.behavioral_evaluation import (
    run_scenario, validate_scenario, write_bundle,
)
from persona_agent.conversation_contracts import ModelReply

SCENARIO_PATH = (
    Path(__file__).resolve().parents[1]
    / "scenarios" / "mvp02_memory_continuity.json"
)
SOURCE = "I've been thinking about walking more regularly."
SCHEDULE = "I haven't settled on a particular walking schedule."
RECALL = "Do you remember what we discussed about walking?"
PARAPHRASE = "What outdoor activity had I been considering?"


class EvidenceObserverModel:
    """Stable, non-claiming reply; inspect actual assembled context separately."""

    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(tuple(messages))
        return ModelReply(
            model_id="mvp02a.evidence-observer",
            text="I can discuss that with you, but I won't invent past details.",
        )


def _scenario():
    return validate_scenario(json.loads(SCENARIO_PATH.read_text(encoding="utf-8")))


def _message(report, text):
    return next(
        item for item in report["transcript"]
        if item["action"] == "message" and item["input"] == text
    )


def _request(report, text):
    matches = [
        item for item in report["model_requests"]
        if item["messages"][-1] == {"role": "user", "content": text}
    ]
    assert len(matches) == 1, (
        f"Expected one model request for {text!r}; observed "
        f"{len(matches)}. Capture diagnostics: "
        f"{[(m.get('route'), m.get('capture_boundary')) for m in report['model_requests']]}"
    )
    return matches[0]


@pytest.fixture(scope="module")
def baseline_report(dsn):
    return run_scenario(dsn, _scenario(), EvidenceObserverModel())


def test_mvp02a_scenario_is_explicitly_reviewable():
    scenario = _scenario()
    assert scenario["name"].startswith("mvp02a-")
    assert [step["name"] for step in scenario["steps"]
            if step["action"] == "session"] == [
        "walking_discussion", "return_visit",
    ]
    assert sum(step["action"] == "restart" for step in scenario["steps"]) == 2
    for text in (RECALL, PARAPHRASE):
        step = next(step for step in scenario["steps"]
                    if step.get("text") == text)
        assert step["mode"] == "model"
        assert step["review_response"]
    # No fabricated memory writes or worker shortcuts are scenario actions.
    assert {step["action"] for step in scenario["steps"]} == {
        "session", "restart", "message",
    }


def test_authentic_source_is_durable_and_not_same_conversation(baseline_report):
    report = baseline_report
    assert not report["errors"], report["errors"]
    assert report["passed"], report["checks"]
    source = _message(report, SOURCE)
    recall = _message(report, RECALL)
    assert source["conversation_id"] != recall["conversation_id"]
    assert source["turn_id"] != recall["turn_id"]
    assert source["response_id"] != recall["response_id"]
    assert report["identity"]["user_id"] and report["identity"]["instance_id"]

    snapshots = [
        row for row in report["snapshots"]
        if row["step_index"] == source["index"]
        and row["checkpoint"] == "after_explicit_memory_worker"
    ]
    assert len(snapshots) == 1
    rows = snapshots[0]["database"]["conversation_turns"]["rows"]
    stored = [row for row in rows if str(row["turn_id"]) == source["turn_id"]]
    assert len(stored) == 1
    assert stored[0]["input_text"] == SOURCE
    assert str(stored[0]["conversation_id"]) == source["conversation_id"]
    assert stored[0]["created_at"]  # date for eventual historical source anchor
    assert _request(report, RECALL)["capture_boundary"] == "pre_gateway_unverified"

    # This baseline does not claim that a passed contract run means recall
    # succeeded. In particular, model responses alone are not source evidence.
    assert report["requires_human_review"]
    assert {item["user"] for item in report["review_items"]} == {
        RECALL, PARAPHRASE,
    }


def test_recall_context_contains_prior_user_source_and_provenance(baseline_report):
    """Desired contract: source + turn ID + date available BEFORE inference."""
    report = baseline_report
    earlier = _message(report, SOURCE)
    context = _request(report, RECALL)["messages"]
    system = "\n".join(msg["content"] for msg in context
                       if msg["role"] == "system")
    latest_user_messages = [
        msg["content"] for msg in context if msg["role"] == "user"
    ]
    assert latest_user_messages[-1] == RECALL
    assert SOURCE not in latest_user_messages  # old session cannot masquerade as current

    # Retrieve the *actual* source date from the observer-only snapshot.
    snapshot = next(row for row in report["snapshots"]
                    if row["step_index"] == earlier["index"]
                    and row["checkpoint"] == "after_explicit_memory_worker")
    source_row = next(row for row in snapshot["database"]["conversation_turns"]["rows"]
                      if str(row["turn_id"]) == earlier["turn_id"])
    source_date = str(source_row["created_at"])[:10]

    assert SOURCE in system, (
        "Missing historical user statement in model system context. "
        "The original turn is durable but not delivered to this session."
    )
    assert earlier["turn_id"] in system, (
        "Recalled quote must include its immutable source-turn ID"
    )
    assert source_date in system, (
        "Recalled quote must include the source-turn date"
    )


def test_host_restart_replays_recall_without_another_inference(dsn):
    observer = EvidenceObserverModel()
    host = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), observer)
    host.begin_session("original")
    host.send(SOURCE, idempotency_key="walking-source")
    host.begin_session("later")
    answer = host.send(RECALL, idempotency_key="walking-recall")
    original_calls = len(observer.calls)
    host.restart_host()
    replay = host.send(RECALL, idempotency_key="walking-recall")
    assert replay.replayed
    assert replay.turn_id == answer.turn_id
    assert replay.response_id == answer.response_id
    assert replay.text == answer.text
    assert len(observer.calls) == original_calls


def test_other_user_cannot_see_prior_user_history(dsn, baseline_report):
    """Negative leakage control: even a directly matching recall cue is scoped."""
    source = _message(baseline_report, SOURCE)
    other = EvidenceObserverModel()
    host = BehavioralAcceptanceHarness(dsn, seed_local_demo(dsn), other)
    host.begin_session("other-user")
    host.send(RECALL, idempotency_key="other-recall")
    assert len(other.calls) == 1
    context = other.calls[0]
    assert all(SOURCE not in msg.content for msg in context)
    assert all(source["turn_id"] not in msg.content for msg in context)


def test_baseline_report_exports_actual_gateway_boundary(tmp_path, baseline_report):
    report = baseline_report
    path = write_bundle(
        report, _scenario(), tmp_path / "mvp02a-baseline.zip",
        model="mvp02a.evidence-observer", git_sha="test",
        migration_hashes={},
    )
    with ZipFile(path) as bundle:
        exported = json.loads(bundle.read("model_requests.json"))
        items = json.loads(bundle.read("review_items.json"))
        transcript = json.loads(bundle.read("transcript.json"))
    assert exported == report["model_requests"]
    assert all(item["capture_boundary"] == "pre_gateway_unverified"
               for item in exported)
    assert len(items) == 2
    assert any(item.get("action") == "message" and item.get("input") == RECALL
               for item in transcript)
