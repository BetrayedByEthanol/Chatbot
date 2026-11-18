"""MVP-02C.10A: v2 dataset, accepted tentative policy and evaluation invariants.

The corpus remains provisional and these tests do not load real models.
"""
import importlib.util
from hashlib import sha256
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_memory_conflicts.py"
V1 = ROOT / "scenarios" / "mvp02c10_memory_conflict_diagnostic.json"
V2 = ROOT / "scenarios" / "mvp02c10_memory_conflict_diagnostic_v2.json"
REWRITES = {"MC035", "MC039", "MC041", "MC048", "MC089"}
TENTATIVE = {"MC071", "MC072", "MC074"}


def cli():
    spec = importlib.util.spec_from_file_location("mvp02c10a_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v1_frozen_and_v2_has_same_ids_with_exactly_five_rewrites():
    e = cli()
    original, v1_hash = e.load_suite(V1)
    revised, v2_hash = e.load_suite(V2)
    assert v1_hash == "2d28d7761920d743addd11e75d2af86b9f229756810bc0fffe6df6e947dc241b"
    assert v1_hash != v2_hash and len(revised) == len(original) == 94
    doc = json.loads(V2.read_text(encoding="utf-8"))
    assert doc["version"] == e.VERSION_V2
    assert doc["annotation_status"] == "DRAFT_POLICY_REVIEWED"
    assert doc["based_on"]["suite_sha256"] == v1_hash
    assert set(doc["based_on"]["rewritten_case_ids"]) == REWRITES
    left, right = ({row["id"]: row for row in dataset} for dataset in (original, revised))
    assert set(left) == set(right)
    changed = set()
    for key in left:
        v1, v2 = left[key], right[key]
        assert v1["expected_nli"] == v2["expected_nli"]
        assert v1["eligibility_reason"] == v2["eligibility_reason"]
        assert v1["review_applicable"] is v2["memory_conflict_eligible"]
        if (v1["stored_text"], v1["candidate_text"]) != (
            v2["stored_text"], v2["candidate_text"]
        ):
            changed.add(key)
    assert changed == REWRITES
    assert doc["based_on"]["review_status"] == (
        "ASSISTANT_SECOND_PASS_SCORE_EXPOSED_NOT_INDEPENDENT_ADJUDICATION"
    )


def test_v2_separates_scope_compatibility_from_admission_and_resolution():
    e = cli()
    rows, _ = e.load_suite(V2)
    by_id = {row["id"]: row for row in rows}
    assert sum(row["scope_compatible"] for row in rows) == 44
    assert sum(row["memory_conflict_eligible"] for row in rows) == 33
    for key in TENTATIVE:
        case = by_id[key]
        assert case["expected_nli"] == "neutral"
        assert case["scope_compatible"] is True
        assert case["stored_claim_status"] == "TENTATIVE_UNADMITTED"
        assert case["memory_conflict_eligible"] is False
        assert case["expected_resolution"] == "TENTATIVE_CLAIM_RESOLVED"
    # Comparable content does not authorize a Memory conflict if source is not admitted.
    assert by_id["MC049"]["scope_compatible"] is True
    assert by_id["MC049"]["stored_claim_status"] == "OTHER_UNADMITTED"
    assert by_id["MC049"]["memory_conflict_eligible"] is False
    assert by_id["MC065"]["scope_compatible"] is False
    assert by_id["MC065"]["stored_claim_status"] == "ADMITTED_ASSERTED"
    assert by_id["MC065"]["memory_conflict_eligible"] is False
    for n in range(79, 87):
        case = by_id[f"MC{n:03}"]
        assert case["expected_resolution"] == "EXPLICIT_USER_CORRECTION"
        assert case["memory_conflict_eligible"] is True
    assert sum(row["expected_resolution"] == "POTENTIAL_MEMORY_CONFLICT"
               for row in rows) == 15
    assert sum(row["expected_resolution"] == "EXPLICIT_USER_CORRECTION"
               for row in rows) == 8
    assert sum(row["expected_resolution"] == "TENTATIVE_CLAIM_RESOLVED"
               for row in rows) == 3


def test_v2_malformed_metadata_fails_closed(tmp_path):
    e = cli()
    doc = json.loads(V2.read_text(encoding="utf-8"))
    by_id = {row["id"]: i for i, row in enumerate(doc["examples"])}
    mutators = (
        (lambda data: data["examples"][by_id["MC071"]].update(
            memory_conflict_eligible=True), "requires admitted source"),
        (lambda data: data["examples"][by_id["MC071"]].update(
            stored_claim_status="ADMITTED_ASSERTED"), "requires admitted source"),
        (lambda data: data["examples"][by_id["MC071"]].update(
            expected_resolution="POTENTIAL_MEMORY_CONFLICT"), "needs eligible contradiction"),
        (lambda data: data["examples"][by_id["MC079"]].update(
            expected_resolution="NONE"), "eligible contradiction"),
        (lambda data: data["examples"][by_id["MC079"]].update(
            scope_compatible=False), "requires admitted source"),
        (lambda data: data["examples"][by_id["MC071"]].update(
            fake_permission=True), "invalid example fields"),
        (lambda data: data.update(annotation_status="REVIEWED_INDEPENDENT"), "annotation status"),
        (lambda data: data.update(version="unknown-v3"), "version"),
    )
    for change, pattern in mutators:
        case = json.loads(json.dumps(doc))
        change(case)
        file = tmp_path / "broken.json"
        file.write_text(json.dumps(case), encoding="utf-8")
        with pytest.raises(ValueError, match=pattern):
            e.load_suite(file)


def test_v2_scoring_keeps_tentative_updates_and_explicit_corrections_separate():
    e = cli()
    rows, _ = e.load_suite(V2)
    wanted = ("MC001", "MC013", "MC071", "MC079", "MC065")
    cases = [next(row for row in rows if row["id"] == key) for key in wanted]
    def scores(contradiction):
        return {"contradiction": contradiction,
                "neutral": 1.0 - contradiction, "entailment": 0.0}
    result = e.score_cases(cases, {
        "MC001": scores(.98),  # genuine conflict
        "MC013": scores(.01),  # comparable restatement, no conflict
        "MC071": scores(.96),  # tentative resolution is NOT an admitted conflict
        "MC079": scores(.85),  # explicit correction still receives review, not via cutoff
        "MC065": scores(.99),  # wrong entity, blocked by oracle
    })
    raw = result["summary"]["raw"]
    oracle = result["summary"]["oracle_eligibility_filter"]
    assert (raw["tp"], raw["fp"], raw["fn"], raw["tn"]) == (1, 2, 1, 1)
    assert (oracle["tp"], oracle["fp"], oracle["fn"], oracle["tn"]) == (1, 0, 1, 3)
    assert result["by_expected_resolution"]["TENTATIVE_CLAIM_RESOLVED"]["raw"]["fp"] == 1
    by_id = {row["id"]: row for row in result["cases"]}
    assert by_id["MC071"]["raw_alert"] is True
    assert by_id["MC071"]["oracle_filtered_alert"] is False
    assert by_id["MC079"]["raw_alert"] is False
    assert by_id["MC079"]["expected_resolution"] == "EXPLICIT_USER_CORRECTION"
    assert by_id["MC079"]["expected_review"] is True


def test_v2_cli_preflights_without_loading_large_models(tmp_path):
    result = run([sys.executable, str(SCRIPT), "--suite", str(V2),
                  "--model-dir", str(tmp_path / "finecat-nli-v2-xl"),
                  "--report", str(tmp_path / "missing.json")],
                 capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "missing locally cached" in result.stderr
    assert not (tmp_path / "missing.json").exists()
