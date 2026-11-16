"""Static preflight contract for the MVP-02C.1 real-model scenario."""
import json
from pathlib import Path

from persona_agent.scenario_validation import validate_scenario


SCENARIO=(
    Path(__file__).resolve().parents[1]
    / "scenarios"
    / "mvp02c1_natural_memory.json"
)


def test_mvp02c1_scenario_is_valid_without_runtime_dependencies():
    scenario=validate_scenario(json.loads(SCENARIO.read_text(encoding="utf-8")))
    reviews=[step for step in scenario["steps"] if step.get("review_response")]
    assert len(reviews)==5
    assert all(1<=len(step["review_response"])<=300 for step in reviews)
    assert scenario["steps"][0]=={
        "action":"session","name":"initial_preferences",
    }
    assert any(
        step.get("text")=="Do you remember what I said about gardening?"
        for step in reviews
    )
    assert any(
        step.get("text")=="I don't enjoy gardening anymore."
        for step in reviews
    )
