"""MVP-02C.2 optional worker's E2E schema is preflight-valid without psycopg."""
import json
from pathlib import Path

from persona_agent.scenario_validation import validate_scenario


SCENARIO=Path(__file__).resolve().parents[1]/"scenarios"/"mvp02c2_reviewed_model_memory.json"


def test_reviewed_model_memory_scenario_validates_before_compose():
    raw=json.loads(SCENARIO.read_text(encoding="utf-8"))
    scenario=validate_scenario(raw)
    assert [step["predicate_key"] for step in scenario["steps"]
            if step["action"]=="approve_model_memory"]==["enjoys_gardening"]
    assert sum(step.get("expect_model_memory_proposed") is True
               for step in scenario["steps"])==1
    assert all(len(step["review_response"])<=300 for step in scenario["steps"]
               if "review_response" in step)
