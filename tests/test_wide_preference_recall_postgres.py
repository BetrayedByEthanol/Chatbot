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
from persona_agent.tool_contracts import TIME_TOOL


SCENARIO=Path(__file__).resolve().parents[1]/"scenarios"/"persona_model_comparison_v1.json"
RECALL="Do you remember my preference about planning and response length?"


class StandInGateway:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="wide-smoke.fake",text="We can continue.")



@pytest.fixture
def isolated_scenario_dsn(dsn):
    """Keep this long scenario independent of prior tests' active pool leases.

    Reuse the already guarded E2E database lifecycle, rather than lowering
    production resource-pool reservations or making the test order-dependent.
    The TEST_DATABASE_URL role must have CREATEDB privilege (as in CI).
    """
    import importlib.util
    import psycopg
    from psycopg.conninfo import make_conninfo

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "persona_e2e_wide_smoke", root / "scripts" / "persona_e2e.py",
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    admin_dsn = make_conninfo(dsn, dbname="postgres")
    db_name, clean_dsn = runner.create_isolated_database(admin_dsn)
    try:
        with psycopg.connect(clean_dsn, autocommit=True) as conn:
            for migration in sorted((root / "migrations").glob("*.sql")):
                with conn.transaction():
                    conn.execute(migration.read_text(encoding="utf-8"))
        yield clean_dsn
    finally:
        runner.drop_isolated_database(admin_dsn, db_name)


def test_wider_25_turn_auto_scenario_recalls_exact_persisted_preferences(isolated_scenario_dsn):
    """Use the frozen scenario and actual post-delivery Relationship worker."""
    dsn = isolated_scenario_dsn
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
    clock=next(x for x in messages if x["input"].startswith("What time is it right now?"))
    assert clock["model_id"]==TIME_TOOL
    assert clock["assistant"].startswith("Current time in ")
    from persona_agent.personal_fact_recall import ROUTE_ID as FACT_ROUTE
    personal=next(x for x in messages if x["input"]=="By the way, what car do I drive?")
    assert personal["model_id"]==FACT_ROUTE
    assert personal["assistant"]=="I don't have a reliable saved answer for that."
    assert model.calls==22  # Clock, preference and personal-fact recall.

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
