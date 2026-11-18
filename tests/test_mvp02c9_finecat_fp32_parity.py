"""Offline unit contracts for the optional FineCat FP32 parity diagnostic."""
from pathlib import Path
from subprocess import run
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_finecat_fp32_parity.py"


def cli():
    spec = importlib.util.spec_from_file_location("finecat_fp32_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matching_logits_meet_tolerance_and_keep_threshold():
    runner = cli()
    result = runner.compare_logits([0., -1., 5.], [0.000002, -1., 5.])
    assert result["passed"]
    assert result["argmax_equal"] and result["threshold_equal"]
    assert result["max_logit_delta"] < 1e-4


def test_score_drift_fails_even_if_class_and_threshold_agree():
    runner = cli()
    result = runner.compare_logits([0., 1., 2.], [0., 1.3, 2.])
    assert not result["passed"]
    assert result["argmax_equal"]
    assert result["threshold_equal"]


def test_threshold_change_fails_and_probabilities_are_stable():
    runner = cli()
    result = runner.compare_logits([0., 0., 3.0], [0., 0., 2.0])
    assert not result["threshold_equal"]
    assert not result["passed"]
    assert sum(runner.probabilities([1000., 1001., 1002.])) == pytest.approx(1.)
    with pytest.raises(ValueError, match="finite"):
        runner.probabilities([float("nan"), 1., 2.])


def test_version_and_label_mapping_fail_closed():
    runner = cli()
    runner.require_transformers_version("4.57.6")
    with pytest.raises(ValueError, match="4.57.6"):
        runner.require_transformers_version("5.23.0")
    assert runner.label_indices({0: "entailment", 1: "neutral", 2: "contradiction"}) == {
        "entailment": 0, "neutral": 1, "contradiction": 2,
    }
    with pytest.raises(ValueError, match="label mapping"):
        runner.label_indices({0: "neutral", 1: "entailment", 2: "contradiction"})


def test_focused_and_full_suites_have_stable_case_ids():
    runner = cli()
    focused, sources = runner.select_cases(False)
    full, _ = runner.select_cases(True)
    assert [case.id for case in focused] == list(runner.FOCUSED_IDS)
    assert len(full) == 174
    assert [source["count"] for source in sources] == [54, 120]
    assert all(len(source["sha256"]) == 64 for source in sources)


def test_cli_help_and_missing_authorization_do_not_load_models(tmp_path):
    help_run = run([sys.executable, str(SCRIPT), "--help"],
                   capture_output=True, text=True, timeout=10)
    assert help_run.returncode == 0, help_run.stderr
    assert "--allow-reviewed-custom-code" in help_run.stdout
    report = tmp_path / "report.json"
    result = run([sys.executable, str(SCRIPT), "--report", str(report)],
                 capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "--allow-reviewed-custom-code" in result.stderr
    assert not report.exists()


def test_no_overwrite_or_invalid_model_directory(tmp_path):
    runner = cli()
    directory = tmp_path / "finecat-nli-v2-xl"
    (directory / "onnx").mkdir(parents=True)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "onnx" / "model.onnx").write_bytes(b"fake")
    report = tmp_path / "present.json"
    report.write_text("original", encoding="utf-8")
    args = [sys.executable, str(SCRIPT), "--model-dir", str(directory),
            "--allow-reviewed-custom-code", "--report", str(report)]
    result = run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert report.read_text(encoding="utf-8") == "original"
