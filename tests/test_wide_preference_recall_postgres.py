"""Wider 25-turn scenario smoke: real auto routing and workers, fake model.

This checks Conversation/Relationship integration across three sessions, not
Ollama response quality. The operator-run real-model benchmark remains separate.
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("psycopg")

from persona_agent.behavioral_evaluation import run_scenario
from persona_agent.conversation_contracts import ModelReply
from persona_agent.preference_recall import ROUTE_ID


SCENARIO=Path(__file__).resolve().parents[1]/"scenarios"/"persona_model_comparison_v1.json"
RECALL="Do you remember my preference about planning and response length?"


class StandInGateway:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="wide-smoke.fake",text="We can continue.")


def test_wider_25_turn_auto_scenario_recalls_exact_persisted_preferences(dsn):
    """Use the frozen scenario and actual post-delivery Relationship worker."""
    scenario=json.loads(SCENARIO.read_text(encoding="utf-8"))
    assert len([s for s in scenario["steps"] if s["action"]=="message"])==25
    assert len([s for s in scenario["steps"] if s["action"]=="session"])==3
    assert len([s for s in scenario["steps"] if s["action"]=="restart"])==2
    for step in scenario["steps"]:
        if step["action"]=="message":
            step["mode"]="auto"

    model=StandInGateway()
    report=run_scenario(dsn,scenario,model)
    assert not report["errors"],report["errors"]
    assert report["contract_passed"],report["checks"]
    messages=[x for x in report["transcript"] if x["action"]=="message"]
    assert len(messages)==25
    assert len({item["conversation_id"] for item in messages})==3
    recall=next(x for x in messages if x["input"]==RECALL)
    assert recall["model_id"]==ROUTE_ID
    assert "not to make big plans" in recall["assistant"]
    assert "one or two sentences" in recall["assistant"]
    assert "unless you request detailed instructions" in recall["assistant"]
    assert all(x["model_id"]!="wide-smoke.fake" for x in (recall,))
    assert model.calls<25  # The real clock and preference routes avoid inference.

    after=next(
        x for x in report["snapshots"]
        if x["checkpoint"]=="after_affect_worker"
        and x["source_turn_id"]==recall["turn_id"]
    )
    recorded=after["database"]["relationship_preferences"]["rows"]
    assert any(
        p["preference_key"]=="RESPONSE_LENGTH"
        and p["preference_value"]=="SHORT"
        and p["status"]=="ACTIVE"
        for p in recorded
    )
    assert after["database"]["relationship_observations"]["count"]>=24
    assert report["snapshots"][-1]["database"]["goal_records"]["count"]==0
