"""MVP-02C.10: offline memory-conflict evaluation contracts (no ML required)."""
import importlib.util
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_memory_conflicts.py"
SUITE = ROOT / "scenarios" / "mvp02c10_memory_conflict_diagnostic.json"


def load_cli():
    spec = importlib.util.spec_from_file_location("mvp02c10_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample():
    return [
        {"id": "X01", "group": "conflict", "expected_nli": "contradiction",
         "review_applicable": True, "eligibility_reason": "SAME_SUBJECT_SCOPE_ASSERTED",
         "stored_text": "I like tea.", "candidate_text": "I do not like tea."},
        {"id": "X02", "group": "evolution", "expected_nli": "neutral",
         "review_applicable": False, "eligibility_reason": "DIFFERENT_TIME",
         "stored_text": "I liked tea in 2020.", "candidate_text": "I dislike tea now."},
        {"id": "X03", "group": "repeat", "expected_nli": "entailment",
         "review_applicable": True, "eligibility_reason": "SAME_SUBJECT_SCOPE_ASSERTED",
         "stored_text": "I enjoy books.", "candidate_text": "I like reading books."},
        {"id": "X04", "group": "condition", "expected_nli": "neutral",
         "review_applicable": False, "eligibility_reason": "NON_ASSERTED_HYPOTHETICAL",
         "stored_text": "If I lived in Rome, I would cycle.", "candidate_text": "I cycle in Rome."},
    ]


def _scores(contradiction, neutral, entailment):
    return {"contradiction": contradiction, "neutral": neutral,
            "entailment": entailment}


def test_draft_suite_covers_memory_specific_failure_slices():
    e = load_cli()
    cases, sha = e.load_suite(SUITE)
    assert len(cases) == 94 and len(sha) == 64
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["expected_nli"] for c in cases} == e.LABELS
    assert sum(c["review_applicable"] and c["expected_nli"] == "contradiction"
               for c in cases) == 23
    assert {"temporal_change", "explicit_correction", "intention_vs_outcome",
            "uncertain_modal", "multilingual_mixed", "attributed_quoted"} <= {
                c["group"] for c in cases
            }
    assert json.loads(SUITE.read_text())["annotation_status"] == "DRAFT_UNREVIEWED"


def test_corrupt_suite_is_rejected(tmp_path):
    e = load_cli()
    original = json.loads(SUITE.read_text())
    for change, phrase in (
        (lambda d: d["examples"][1].update(id=d["examples"][0]["id"]), "duplicate"),
        (lambda d: d["examples"][0].update(fake=True), "fields"),
        (lambda d: d["examples"][0].update(review_applicable=False), "inconsistent"),
        (lambda d: d["examples"][0].update(expected_nli="unknown"), "gold label"),
        (lambda d: d.update(version="bad"), "version"),
        (lambda d: d.update(annotation_status="APPROVED"), "annotation status"),
    ):
        doc = json.loads(json.dumps(original))
        change(doc)
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(doc))
        with pytest.raises(ValueError, match=phrase):
            e.load_suite(path)


def test_raw_vs_oracle_filter_distinguishes_false_alert_from_model_error():
    e = load_cli()
    cases = _sample()
    predictions = {
        "X01": _scores(.98, .01, .01),  # true conflict
        "X02": _scores(.95, .04, .01),  # ineligible temporal false alarm
        "X03": _scores(.01, .01, .98),  # entailment
        "X04": _scores(.60, .39, .01),  # argmax contradiction, below cutoff
    }
    report = e.score_cases(cases, predictions)
    raw = report["summary"]["raw"]
    gated = report["summary"]["oracle_eligibility_filter"]
    assert [raw[k] for k in ("tp", "fp", "fn", "tn")] == [1, 1, 0, 2]
    assert [gated[k] for k in ("tp", "fp", "fn", "tn")] == [1, 0, 0, 3]
    assert report["summary"]["raw_false_alert_ids"] == ["X02"]
    assert report["summary"]["oracle_false_alert_ids"] == []
    assert report["summary"]["nli_argmax_correct"] == 2  # X02/X04 are not neutral
    assert report["by_group"]["evolution"]["raw"]["fp"] == 1
    assert report["cases"][3]["raw_alert"] is False


def test_fixed_cutoff_is_inclusive_and_false_negative_visible():
    e = load_cli()
    cases = _sample()
    p = {
        "X01": _scores(.899, .100, .001),
        "X02": _scores(.900, .099, .001),
        "X03": _scores(.001, .001, .998),
        "X04": _scores(.10, .89, .01),
    }
    data = e.score_cases(cases, p)
    assert data["summary"]["raw"]["fn"] == 1
    assert data["summary"]["raw"]["fp"] == 1
    assert data["summary"]["oracle_eligibility_filter"]["fn"] == 1


def test_invalid_scores_and_ids_fail_closed():
    e = load_cli()
    cases = _sample()
    predictions = {c["id"]: _scores(.1, .8, .1) for c in cases}
    del predictions["X01"]
    with pytest.raises(ValueError, match="IDs"):
        e.score_cases(cases, predictions)
    predictions["X01"] = _scores(float("nan"), 0.8, 0.1)
    with pytest.raises(ValueError, match="normalized"):
        e.score_cases(cases, predictions)


def test_fake_adapter_evaluates_one_variant_without_loading_models(tmp_path):
    e = load_cli()
    graph = tmp_path / "model.onnx"
    graph.write_bytes(b"fixture")
    cases = _sample()
    class FakeModel:
        def __init__(self, config):
            self.model_file = graph
            self.ort_version = "offline-test"
            self.labels = {"entailment": 0, "neutral": 1, "contradiction": 2}
        def nli(self, stored, candidate):
            idx = next(i for i, c in enumerate(cases)
                       if c["stored_text"] == stored and c["candidate_text"] == candidate)
            return [_scores(.98, .01, .01), _scores(.95, .04, .01),
                    _scores(.01, .01, .98), _scores(.01, .98, .01)][idx]
    output = e.evaluate_variant(cases, tmp_path, "int8", 2, 2, 1,
                                model_factory=FakeModel)
    assert output["precision"] == "int8"
    assert output["summary"]["raw"]["fp"] == 1
    assert len(output["cases"]) == 4
    assert output["p95_pair_ms"] >= 0
    assert output["model_bytes_graph_only"] == len(b"fixture")


def test_variant_drift_tracks_threshold_and_argmax():
    e = load_cli()
    cases = _sample()
    baseline = e.score_cases(cases, {
        "X01": _scores(.91, .08, .01), "X02": _scores(.91, .08, .01),
        "X03": _scores(.01, .01, .98), "X04": _scores(.01, .98, .01),
    })
    altered = e.score_cases(cases, {
        "X01": _scores(.89, .10, .01), "X02": _scores(.20, .79, .01),
        "X03": _scores(.01, .01, .98), "X04": _scores(.01, .98, .01),
    })
    result = e.compare_variants({"precision": "fp32", **baseline},
                                {"precision": "int8", **altered})
    assert result["raw_threshold_changes"] == ["X01", "X02"]
    assert result["oracle_filter_changes"] == ["X01"]
    assert result["argmax_changes"] == ["X02"]


def test_cli_help_and_overwrite_guard_without_optional_dependencies(tmp_path):
    help_result = run([sys.executable, str(SCRIPT), "--help"],
                      text=True, capture_output=True, timeout=10)
    assert help_result.returncode == 0, help_result.stderr
    assert "--precisions" in help_result.stdout
    path = tmp_path / "existing.json"
    path.write_text("do not overwrite")
    result = run([sys.executable, str(SCRIPT), "--report", str(path)],
                 text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert path.read_text() == "do not overwrite"


def test_preflight_rejects_missing_cache_before_inference(tmp_path):
    path = tmp_path / "missing.json"
    model = tmp_path / "finecat-nli-v2-xl"
    result = run([sys.executable, str(SCRIPT), "--suite", str(SUITE),
                  "--model-dir", str(model), "--report", str(path)],
                 text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "missing locally cached" in result.stderr
    assert not path.exists()
