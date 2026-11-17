"""MVP-02C.7: offline scoring and draft diagnostic suite contracts."""
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

from persona_agent.nli_evaluation import LABELS, load_nli_suite, summarize_nli

ROOT = Path(__file__).resolve().parents[1]
NEW = ROOT / "scenarios" / "mvp02c7_nli_threshold_diagnostics.json"
OLD = ROOT / "scenarios" / "mvp02c5_nli_comparison.json"


def _scores(label: str, probability: float = 0.98) -> dict[str, float]:
    return {
        name: probability if name == label else (1 - probability) / 2
        for name in LABELS
    }


def test_draft_suite_is_distinct_bounded_and_covers_failure_modes():
    cases, digest = load_nli_suite(NEW)
    prior, _ = load_nli_suite(OLD)
    assert len(cases) == 120 and len(digest) == 64
    assert {label: sum(p.label == label for p in cases) for label in LABELS} == {
        "contradiction": 24, "entailment": 24, "neutral": 72
    }
    pairs = {(p.premise, p.hypothesis) for p in cases}
    assert len(pairs) == len(cases)
    assert not pairs.intersection((p.premise, p.hypothesis) for p in prior)
    groups = {p.group for p in cases}
    assert {"temporal_scope", "intention_vs_action", "scope_mismatch",
            "german_neutral", "cross_language_contradiction",
            "contradiction_preference"} <= groups
    document = json.loads(NEW.read_text(encoding="utf-8"))
    assert "pending independent review" in document["provenance"]


def test_fixed_threshold_marks_both_accepted_and_rejected_alerts():
    cases, _ = load_nli_suite(NEW)
    scores = {p.id: _scores(p.label) for p in cases}
    scores["HN001"] = _scores("contradiction", 0.95)  # false alert
    scores["HN002"] = _scores("contradiction", 0.70)  # argmax false; below 0.90
    scores["HC001"] = _scores("neutral", 0.98)       # missed contradiction
    report = summarize_nli(cases, scores)
    c = report["contradiction_threshold"]
    assert c == {
        "score_cutoff": 0.9, "true_positive": 23, "false_positive": 1,
        "false_negative": 1, "true_negative": 95,
        "precision": round(23 / 24, 4), "recall": round(23 / 24, 4),
        "flagged_total": 24,
        "false_alert_rate_noncontradiction": round(1 / 96, 4),
    }
    assert report["false_contradiction_count"] == 2
    assert report["high_confidence_false_contradictions"] == 1
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["HN001"]["contradiction_at_threshold"] is True
    assert by_id["HN001"]["contradiction_threshold_outcome"] == "FP"
    assert by_id["HN002"]["predicted"] == "contradiction"
    assert by_id["HN002"]["contradiction_at_threshold"] is False
    assert by_id["HN002"]["contradiction_threshold_outcome"] == "TN"
    assert by_id["HC001"]["contradiction_threshold_outcome"] == "FN"
    assert by_id["HC002"]["contradiction_threshold_outcome"] == "TP"


def test_threshold_is_inclusive_and_never_a_permission_grant():
    cases, _ = load_nli_suite(NEW)
    scores = {p.id: _scores(p.label) for p in cases}
    scores["HN001"] = _scores("contradiction", 0.9)
    report = summarize_nli(cases, scores)
    assert report["contradiction_threshold"]["false_positive"] == 1
    assert report["cases"][24]["contradiction_at_threshold"] is True
    with pytest.raises(ValueError, match="threshold"):
        summarize_nli(cases, scores, high_confidence_threshold=1.0)


def test_native_benchmark_cli_exposes_new_suite_without_loading_models():
    path = ROOT / "scripts" / "compare_local_nli_native.py"
    result = run([sys.executable, str(path), "--help"],
                 capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "mvp02c7_nli_threshold_diagnostics.json" in result.stdout
    assert "--allow-reviewed-custom-code" in result.stdout
