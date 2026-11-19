"""MVP-02C.14G — selection analysis contracts; no real models in unit tests."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
mod = importlib.import_module("analyze_semantic_benchmark_results")
probe = importlib.import_module("evaluate_semantic_models")
benchmark = importlib.import_module("benchmark_semantic_models")


def _fixtures(tmp_path: Path, name: str, *, error_claims=0,
              support=1, correct=1, case_id="SD001") -> tuple[Path, Path]:
    assert not support or error_claims == 0
    metrics = {
        key: {
            "eligible": 1, "supported": support,
            "correct": correct if support else 0,
            "incorrect": support - correct if support else 0,
            "abstained": 1 - support, "unscored_reference": 0,
        }
        for key in probe.LABEL_FIELDS
    }
    score = {
        "version": probe.VERSION,
        "prompt_version": probe.PROMPT_VERSION,
        "reference_mode": mod.REFERENCE_MODE,
        "annotation_status": mod.ANNOTATION_STATUS,
        "backend": name,
        "suite_sha256": "0" * 64,
        "sample_cases": 1,
        "sample_claims": 1,
        "error_claims": error_claims,
        "by_dimension": metrics,
        "cases": [{"id": case_id, "reference_claims": 1,
                   "errors": error_claims}],
        "median_claim_inference_ms": 100.0,
    }
    profile = {
        "version": benchmark.VERSION,
        "prompt_version": probe.PROMPT_VERSION,
        "reference_mode": mod.REFERENCE_MODE,
        "annotation_status": mod.ANNOTATION_STATUS,
        "backend": name,
        "suite_sha256": "0" * 64,
        "sample_cases": 1,
        "sample_claims": 1,
        "case_ids": [case_id],
        "model": {"model_name": name},
        "warmup": {"errors": 1 if error_claims else 0},
        "timing": {"median_suite_ms": 150.0},
        "runs": [
            {"index": 1, "error_claims": error_claims, "by_dimension": metrics},
            {"index": 2, "error_claims": error_claims, "by_dimension": metrics},
        ],
    }
    paths = (tmp_path / f"{name}-score.json",
             tmp_path / f"{name}-profile.json")
    for path, obj in zip(paths, (score, profile)):
        path.write_text(json.dumps(obj), encoding="utf-8")
    return paths


def test_all_failed_model_runs_are_not_called_semantic_inaccuracy(tmp_path):
    a = _fixtures(tmp_path, "spacy", support=1)
    b = _fixtures(tmp_path, "qwen", error_claims=1, support=0, correct=0)
    report = mod.analyze_pairs([a, b], artifact_run_id=44516887227)
    assert report["decision"] == "NO_MODEL_SELECTED"
    assert report["source_run_id"] == 44516887227
    assert report["backends"][0]["output_status"] == "NO_MODEL_ERRORS_OBSERVED"
    assert report["backends"][1]["output_status"] == "NO_VALID_MODEL_OUTPUT"
    assert report["backends"][1]["errors_per_run"] == [1, 1]
    assert "ALL_CLASSIFICATIONS_FAILED" in report["backends"][1]["blockers"]
    assert "MODEL_OUTPUT_RELIABILITY_BELOW_PROPOSAL" in report["backends"][1]["blockers"]
    assert report["backends"][1]["dimensions"]["certainty"]["coverage"] == 0.0
    assert report["backends"][1]["dimensions"]["certainty"]["conditional_draft_agreement"] is None
    assert report["backends"][0]["dimensions"]["certainty"]["conditional_draft_agreement"] == 1.0
    assert report["proposed_selection_gates"]["status"] == "PROPOSAL_NOT_APPROVED_FOR_ROUTING"
    assert report["backends"][0]["selection_status"] == "NOT_ELIGIBLE_FOR_PRODUCTION_SELECTION"
    assert "sample text" not in json.dumps(report)


def test_refuses_mixed_cases_even_if_suite_hash_matches(tmp_path):
    a = _fixtures(tmp_path, "a", case_id="SD001")
    b = _fixtures(tmp_path, "b", case_id="SD002")
    with pytest.raises(ValueError, match="not paired"):
        mod.analyze_pairs([a, b])


def test_refuses_duplicate_provider_identity(tmp_path):
    a = _fixtures(tmp_path, "a")
    with pytest.raises(ValueError, match="duplicate backend"):
        mod.analyze_pairs([a, a])


def test_refuses_false_success_or_score_profile_mismatch(tmp_path):
    a = _fixtures(tmp_path, "a")
    b = _fixtures(tmp_path, "b")
    profile = json.loads(a[1].read_text())
    profile["runs"][0]["by_dimension"]["certainty"]["supported"] = 0
    a[1].write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="inconsistent|differs"):
        mod.analyze_pairs([a, b])


def test_rejects_run_scoring_disagreement_not_masked_by_repeat_averages(tmp_path):
    a = _fixtures(tmp_path, "a")
    b = _fixtures(tmp_path, "b")
    profile = json.loads(a[1].read_text())
    m = profile["runs"][1]["by_dimension"]["certainty"]
    m.update({"supported": 0, "correct": 0, "abstained": 1})
    a[1].write_text(json.dumps(profile))
    result = mod.analyze_pairs([a, b])
    assert result["decision"] == "NO_MODEL_SELECTED"
    # First-run score isn't silently merged with second-run differences.
    assert result["backends"][0]["dimensions"]["certainty"]["correct"] == 1


def test_rejects_unexpected_report_version_and_unsupported_dimensions(tmp_path):
    a = _fixtures(tmp_path, "a")
    b = _fixtures(tmp_path, "b")
    raw = json.loads(a[0].read_text())
    raw["version"] = "other"
    a[0].write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="contract"):
        mod.analyze_pairs([a, b])


def test_write_once_cli_and_text_free_output(tmp_path):
    a = _fixtures(tmp_path, "a")
    b = _fixtures(tmp_path, "b")
    target = tmp_path / "analysis.json"
    args = [
        "--pair", str(a[0]), str(a[1]),
        "--pair", str(b[0]), str(b[1]),
        "--run-id", "44516887227", "--report", str(target),
    ]
    assert mod.main(args) == 0
    report = json.loads(target.read_text())
    assert report["source_run_id"] == 44516887227
    assert report["backends"][0]["score_sha256"]
    assert report["backends"][0]["profile_sha256"]
    first = target.read_text()
    with pytest.raises(SystemExit):
        mod.main(args)
    assert target.read_text() == first


def test_requires_two_comparable_local_backends(tmp_path):
    only = _fixtures(tmp_path, "a")
    with pytest.raises(ValueError, match="2..5"):
        mod.analyze_pairs([only])
