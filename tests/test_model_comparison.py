"""Deterministic, no-Docker tests for reproducible model comparison."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compare_persona_models as comparison
from persona_agent.scenario_validation import validate_scenario

SCENARIO_PATH = ROOT / "scenarios" / "persona_model_comparison_v1.json"
REQUIRED_EXTRA_MEMBERS = {
    "scenario.json": {"name": "synthetic-test"},
    "model_requests.json": [],
    "interpretation_diagnostics.json": [],
    "semantic_diagnostics.json": [],
    "review_items.json": [],
    "delivery_events.json": [],
}


def test_scenario_has_25_natural_turns_and_three_sessions():
    scenario = validate_scenario(json.loads(SCENARIO_PATH.read_text()))
    steps = scenario["steps"]
    assert len([x for x in steps if x["action"] == "message"]) == 25
    assert len([x for x in steps if x["action"] == "session"]) == 3
    assert len([x for x in steps if x["action"] == "restart"]) == 2
    assert len({x["review_case"]["id"] for x in steps if x["action"] == "message"}) == 25


def test_mode_variants_preserve_identical_user_text_and_rubric():
    original = json.loads(SCENARIO_PATH.read_text())
    source = deepcopy(original)
    model = comparison.variant(source, "model-only")
    full = comparison.variant(source, "full")
    assert source == original  # No in-place changes across models.
    m = [s for s in model["steps"] if s["action"] == "message"]
    a = [s for s in full["steps"] if s["action"] == "message"]
    assert [x["text"] for x in m] == [x["text"] for x in a]
    assert [x["review_case"] for x in m] == [x["review_case"] for x in a]
    assert all(x["mode"] == "model" for x in m)
    assert all(x["mode"] == "auto" for x in a)


@pytest.mark.parametrize("bad", [
    "", "single", "same,same", "one,two,three,four,five,six",
    "--evil,good", "good,bad;rm", "one,,two", "a,b/../../secret",
])
def test_reject_invalid_models_before_launch(bad):
    with pytest.raises(ValueError):
        comparison.parse_models(bad)


def test_ollama_namespace_and_tag_are_supported():
    assert comparison.parse_models("qwen2.5:7b,namespace/qwen3.5:latest") == [
        "qwen2.5:7b", "namespace/qwen3.5:latest",
    ]


def test_read_summary_uses_observed_routes_not_assumed_model(tmp_path):
    bundle = tmp_path / "test.zip"
    members = {
        "manifest.json": {"model": "test-model", "synthetic_data": True},
        "transcript.json": [
            {"action": "session", "name": "first"},
            {"action": "message", "assistant": "One short reply",
             "response_elapsed_ms": 8.0, "model_id": "test-model"},
            {"action": "message", "assistant": "Current time unavailable",
             "response_elapsed_ms": 12.0, "model_id": "time.rule.v1"},
        ],
        "state_snapshots.json": [
            {"application": {"counts": {"goals": 0, "memories": 2}}}
        ],
        "evaluation.json": {"contract_passed": True, "behavioral_checks_passed": True},
        "errors.json": [],
    }
    members.update(REQUIRED_EXTRA_MEMBERS)
    with ZipFile(bundle, "w") as z:
        for name, value in members.items():
            z.writestr(name, json.dumps(value))
    result = comparison.read_summary(bundle)
    assert result["delivered_message_count"] == 2
    assert result["avg_response_ms"] == 10.0
    assert result["model_route_counts"] == {"test-model": 1, "time.rule.v1": 1}
    assert result["last_scoped_counts"]["goals"] == 0
    assert result["output_tokens"] is None
    assert result["quality_status"] == "AWAITING_INDEPENDENT_HUMAN_REVIEW"


@pytest.mark.parametrize("thinking",["default","on","off"])
def test_comparison_runs_isolated_jobs_and_generates_blind_packets(tmp_path, monkeypatch, thinking):
    class NoShuffle:
        def shuffle(self, seq):
            pass

    monkeypatch.setattr(comparison.secrets, "SystemRandom", lambda: NoShuffle())
    commands = []
    packet = {
        "protocol": "test-blind",
        "cases": [{"case_id": "walk", "user": "Synthetic question",
                   "assistant": "Synthetic reply"}],
    }

    def fake_run(command, cwd, check):
        commands.append(command)
        bundle = Path(command[command.index("--report") + 1])
        bundle.write_bytes(b"fake-zip")  # read_summary and packet parser patched below.
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(comparison.subprocess, "run", fake_run)
    monkeypatch.setattr(comparison, "packet_from_bundle", lambda bundle: packet)
    monkeypatch.setattr(comparison, "rating_template", lambda packet, reviewer:
                        {"reviewer_id": reviewer, "ratings": []})
    monkeypatch.setattr(comparison, "read_summary", lambda bundle: {
        "contract_passed": True, "error_count": 0, "delivered_message_count": 25,
    })
    output_dir = tmp_path / "comparison"
    result = comparison.compare([
        "--models", "qwen2.5:7b,qwen3.5:latest",
        "--scenario", str(SCENARIO_PATH),
        "--output-dir", str(output_dir),
        "--mode", "both", "--thinking", thinking,
    ])
    assert result == 0
    assert len(commands) == 4
    if thinking == "default":
        assert all("--thinking" not in cmd for cmd in commands)
    else:
        assert all(cmd[cmd.index("--thinking") + 1] == thinking
                   for cmd in commands)
    assert all(cmd[:2] == ["bash", "scripts/run_persona_e2e.sh"] for cmd in commands)
    # Every job starts in a new database via the existing runner; no one DB
    # reused across model/mode pairs.
    assert len({cmd[cmd.index("--report") + 1] for cmd in commands}) == 4
    summaries = json.loads((output_dir / "summary.json").read_text())
    assert len(summaries["results"]) == 4
    assert all(item["review_packet"] for item in summaries["results"])
    assert summaries["quality_winner"] is None
    model_map = json.loads((output_dir / "PRIVATE_model_map.json").read_text())
    assert {v["model"] for v in model_map["mapping"].values()} == {
        "qwen2.5:7b", "qwen3.5:latest",
    }
    assert model_map["gateway_configuration"]["thinking"] == thinking
    assert all(item["thinking"] == thinking for item in summaries["results"])
    for key in model_map["mapping"]:
        packet_content = (output_dir / f"{key}.review.json").read_text()
        assert "qwen" not in packet_content
    assert output_dir.stat().st_mode & 0o077 == 0
    assert (output_dir / "PRIVATE_model_map.json").stat().st_mode & 0o077 == 0


def test_contract_failure_never_emits_blind_review(tmp_path, monkeypatch):
    def fake_run(command, cwd, check):
        Path(command[command.index("--report") + 1]).write_bytes(b"fake")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(comparison.subprocess, "run", fake_run)
    monkeypatch.setattr(comparison, "read_summary", lambda bundle: {
        "contract_passed": False, "error_count": 1,
    })
    output_dir = tmp_path / "failed"
    code = comparison.compare([
        "--models", "one,two", "--scenario", str(SCENARIO_PATH),
        "--output-dir", str(output_dir),
    ])
    assert code == 1
    assert list(output_dir.glob("*.review.json")) == []
    assert json.loads((output_dir / "summary.json").read_text())["quality_winner"] is None


def test_refuse_overwrite_prior_evidence(tmp_path, monkeypatch):
    output_dir = tmp_path / "already_present"
    output_dir.mkdir()
    with pytest.raises(FileExistsError):
        comparison.compare([
            "--models", "one,two", "--output-dir", str(output_dir),
            "--scenario", str(SCENARIO_PATH),
        ])


@pytest.mark.parametrize("evidence", ["missing", "invalid_zip"])
def test_unconfirmed_status_one_publication_aborts_remaining_models(
    tmp_path, monkeypatch, evidence,
):
    class NoShuffle:
        def shuffle(self, seq):
            pass

    monkeypatch.setattr(comparison.secrets, "SystemRandom", lambda: NoShuffle())
    commands = []

    def fake_run(command, cwd, check):
        commands.append(command)
        bundle = Path(command[command.index("--report") + 1])
        if evidence == "invalid_zip":
            bundle.write_bytes(b"partial ZIP bytes")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(comparison.subprocess, "run", fake_run)
    output = tmp_path / evidence
    code = comparison.compare([
        "--models", "model_a,model_b", "--scenario", str(SCENARIO_PATH),
        "--output-dir", str(output),
    ])
    assert code == 1
    assert len(commands) == 1, "Must not launch model B against the retained port"
    result = json.loads((output / "summary.json").read_text())["results"]
    assert len(result) == 1
    assert result[0]["exit_code"] == 1
    assert result[0]["evidence_status"] == (
        "MISSING" if evidence == "missing" else "INVALID"
    )
    assert not list(output.glob("*.review.json"))


def test_published_status_one_contract_failure_allows_next_model(tmp_path, monkeypatch):
    class NoShuffle:
        def shuffle(self, seq):
            pass

    monkeypatch.setattr(comparison.secrets, "SystemRandom", lambda: NoShuffle())
    commands = []

    def fake_run(command, cwd, check):
        commands.append(command)
        bundle = Path(command[command.index("--report") + 1])
        first = len(commands) == 1
        members = {
            "manifest.json": {
                "synthetic_data": True, "model": command[command.index("--model") + 1],
            },
            "transcript.json": [
                {"action": "message", "assistant": "A synthetic reply",
                 "response_elapsed_ms": 2, "model_id": "fake"},
            ],
            "state_snapshots.json": [],
            "evaluation.json": {
                "contract_passed": not first,
                "behavioral_checks_passed": not first,
            },
            "errors.json": [{"type": "AssertionError"}] if first else [],
        }
        members.update(REQUIRED_EXTRA_MEMBERS)
        with ZipFile(bundle, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, json.dumps(value))
        return SimpleNamespace(returncode=1 if first else 0)

    monkeypatch.setattr(comparison.subprocess, "run", fake_run)
    monkeypatch.setattr(comparison, "packet_from_bundle", lambda bundle: {
        "protocol": "test-blind", "cases": [],
    })
    monkeypatch.setattr(comparison, "rating_template", lambda packet, reviewer: {
        "reviewer_id": reviewer, "ratings": [],
    })
    output = tmp_path / "published_contract_failure"
    code = comparison.compare([
        "--models", "model_a,model_b", "--scenario", str(SCENARIO_PATH),
        "--output-dir", str(output),
    ])
    assert code == 1  # Contract failure remains a failed comparison.
    assert len(commands) == 2  # But published evidence makes next run safe.
    results = json.loads((output / "summary.json").read_text())["results"]
    assert len(results) == 2
    assert [item["evidence_status"] for item in results] == [
        "VERIFIED", "VERIFIED",
    ]
    assert results[0]["exit_code"] == 1
    assert results[0]["review_packet"] is None
    assert results[1]["review_packet"] is not None


def test_status_two_always_stops_even_with_readable_bundle(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, cwd, check):
        commands.append(command)
        bundle = Path(command[command.index("--report") + 1])
        with ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps({
                "synthetic_data": True, "model": "fake",
            }))
            archive.writestr("transcript.json", "[]")
            archive.writestr("state_snapshots.json", "[]")
            archive.writestr("evaluation.json", '{"contract_passed":false}')
            archive.writestr("errors.json", "[]")
            for name, value in REQUIRED_EXTRA_MEMBERS.items():
                archive.writestr(name, json.dumps(value))
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(comparison.subprocess, "run", fake_run)
    result = comparison.compare([
        "--models", "model_a,model_b", "--scenario", str(SCENARIO_PATH),
        "--output-dir", str(tmp_path / "status_two"),
    ])
    assert result == 1
    assert len(commands) == 1


@pytest.mark.parametrize("thinking,expected",[
    ("default",None),
    ("on","on"),
    ("off","off"),
])
@pytest.mark.parametrize("direct",[False,True])
def test_build_command_passes_thinking_to_correct_e2e_runner(thinking,expected,direct):
    command=comparison.build_command(
        "full","qwen3.5:9b",Path("scenario.json"),Path("results.zip"),
        "http://127.0.0.1:11434",direct,thinking,
    )
    assert command[:2] == (
        [sys.executable,"scripts/persona_e2e.py"]
        if direct else ["bash","scripts/run_persona_e2e.sh"]
    )
    if expected is None:
        assert "--thinking" not in command
    else:
        assert command[command.index("--thinking")+1]==expected
    assert command[command.index("--model")+1]=="qwen3.5:9b"


def test_invalid_thinking_flag_is_rejected_before_model_launch(tmp_path,monkeypatch):
    def unexpected(*args,**kwargs):
        raise AssertionError("model runner must not start")
    monkeypatch.setattr(comparison.subprocess,"run",unexpected)
    with pytest.raises(SystemExit) as exc:
        comparison.compare([
            "--models","qwen2.5:7b,qwen3.5:9b",
            "--thinking","sometimes",
            "--output-dir",str(tmp_path/"invalid"),
        ])
    assert exc.value.code==2
    assert not (tmp_path/"invalid").exists()
