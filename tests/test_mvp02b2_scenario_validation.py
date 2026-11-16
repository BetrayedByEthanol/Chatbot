"""Ensure the published MVP-02B.2 real-model scenario is executable.

Unlike PostgreSQL host tests, this catches static scenario-validation failures
before the expensive E2E Compose/database/model pipeline is started.
"""
import json
from pathlib import Path

from persona_agent.scenario_validation import validate_scenario


SCENARIO = (
    Path(__file__).resolve().parents[1]
    / "scenarios"
    / "mvp02b2_context_recall.json"
)


def test_mvp02b2_scenario_passes_runner_validation():
    scenario = validate_scenario(json.loads(SCENARIO.read_text(encoding="utf-8")))
    reviews = [
        step for step in scenario["steps"] if step.get("review_response")
    ]
    assert len(reviews) == 3
    assert all(len(step["review_response"]) <= 300 for step in reviews)
    assert reviews[0]["text"] == "What outdoor activity had I been considering?"
    assert reviews[1]["text"] == "Do you remember what we discussed about walking?"
    assert scenario["steps"][0] == {
        "action": "session", "name": "original_walking_discussion",
    }
