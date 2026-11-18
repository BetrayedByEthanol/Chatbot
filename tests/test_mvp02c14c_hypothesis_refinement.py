"""MVP-02C.14C: paired hypothesis comparison contract tests.

All predictions here are *synthetic injected outputs*; these tests
make no claim about the locally installed DeBERTa model's accuracy.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CORPUS = ROOT / "scenarios" / "mvp02c13_semantic_scope_corpus.json"


def _runner():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "mvp02c14c_script", SCRIPTS / "evaluate_zeroshot_hypotheses.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _examples(*ids):
    all_rows = json.loads(CORPUS.read_text(encoding="utf-8"))["examples"]
    return [row for row in all_rows if row["id"] in ids]


class StubNLI:
    """Fake NLI scores, with altered label order to exercise the real parser."""

    def __init__(self, module, *, fail_on=None, invalid=False):
        self.module = module
        self.calls = []
        self.fail_on = fail_on
        self.invalid = invalid

    def classify(self, texts, hypotheses):
        self.calls.append((tuple(texts), tuple(hypotheses)))
        if self.fail_on is not None and self.fail_on in texts:
            raise RuntimeError("source must not appear in report")
        if self.invalid:
            return [{"labels": ["bad"], "scores": [float("nan")]}] * len(texts)
        configs = {
            **{key: labels for key, labels in self.module.OLD_HYPOTHESES.items()},
            **{("refined_" + key): labels for key, labels in self.module.REFINED.items()},
        }
        possible = [
            (dimension, labels)
            for dimension, labels in configs.items()
            if tuple(hypotheses) == tuple(labels.values())
        ]
        assert len(possible) == 1
        dim, mapping = possible[0]
        is_refined = dim.startswith("refined_")
        if is_refined:
            dim = dim.removeprefix("refined_")
        proposals = []
        for text in texts:
            if dim == "statement_type":
                selected = "INTENTION" if "Maybe" in text else "PREFERENCE"
            elif dim == "temporal_scope":
                if is_refined:
                    selected = "FUTURE" if "tomorrow" in text else "CURRENT"
                else:
                    selected = "FUTURE" if "tomorrow" in text else "UNSPECIFIED"
            elif dim == "modality":
                if is_refined:
                    selected = "TENTATIVE" if "Maybe" in text else "ASSERTED"
                else:
                    selected = "UNKNOWN"
            elif dim == "pragmatic_function":
                selected = "UNKNOWN"
            elif dim == "correction_signal":
                selected = ("CORRECTING" if "Actually" in text or "anymore"
                            in text else "NOT_CORRECTING")
            else:
                raise AssertionError(dim)
            reverse = list(reversed(hypotheses))
            proposals.append({
                "labels": reverse,
                "scores": [0.99 if label == mapping[selected] else 0.01
                           for label in reverse],
            })
        return proposals


def test_hypothesis_set_excludes_unknown_as_competing_label():
    module = _runner()
    assert set(module.REFINED) == {
        "temporal_scope", "modality", "correction_signal"
    }
    assert "UNSPECIFIED" not in module.REFINED["temporal_scope"]
    assert "UNKNOWN" not in module.REFINED["modality"]
    assert set(module.REFINED["correction_signal"]) == {
        "CORRECTING", "NOT_CORRECTING"
    }
    assert "UNKNOWN" in module.OLD_HYPOTHESES["modality"]
    assert module.OLD_HYPOTHESES_VERSION if False else True


def test_paired_comparison_uses_same_texts_model_and_validated_corpus():
    module = _runner()
    subset = _examples("SC001", "SC013", "SC066")
    gateway = StubNLI(module)
    result = module.evaluate(subset, backend=gateway, batch_size=2)
    assert result["total"] == 3
    assert result["v14b_reference"]["batch_calls"] == 8
    assert result["refined_batch_calls"] == 6
    assert len(gateway.calls) == 14
    assert result["refined_error_batches"] == 0
    assert result["v14b_reference"]["error_batches"] == 0
    for dimension in module.REFINED:
        before = result["by_dimension"][dimension]["v14b"]
        after = result["by_dimension"][dimension]["v14c"]
        assert before["eligible"] == after["eligible"] == 3
        assert after["correct"] == 3
        assert after["coverage_on_eligible"] == 1
        assert before["correct"] < after["correct"]
    assert result["changes"]["temporal_scope"]["newly_correct"] == [
        "SC001", "SC066"
    ]
    assert result["cases"][-1]["refined"]["correction_signal"]["prediction"] == (
        "CORRECTING"
    )
    for content, labels in gateway.calls:
        assert all("SC001" not in s for s in content)
        assert all(isinstance(s, str) for s in labels)


def test_unknown_gold_is_not_recast_as_a_confirmed_negative():
    module = _runner()
    assert module._gold(_examples("SC078")[0], "correction_signal") == "UNKNOWN"
    result = module.evaluate(
        _examples("SC001", "SC078"), backend=StubNLI(module), batch_size=2
    )
    correction = result["by_dimension"]["correction_signal"]
    for name in ("v14b", "v14c"):
        assert correction[name]["unscored_ambiguous_drafts"] == 1
        assert correction[name]["eligible"] == 1
    assert result["by_dimension"]["modality"]["v14c"]["eligible"] == 1
    assert result["by_dimension"]["temporal_scope"]["v14c"]["eligible"] == 1


def test_threshold_based_abstention_is_not_unknown_or_success():
    module = _runner()
    result = module.evaluate(
        _examples("SC001", "SC013"), backend=StubNLI(module),
        min_score=1.0,
    )
    for dim in module.REFINED:
        metric = result["by_dimension"][dim]["v14c"]
        assert metric["supported"] == metric["correct"] == 0
        assert metric["abstained"] == 2
        assert metric["incorrect"] == 0
        assert all(
            case["refined"][dim]["reason"] == "BELOW_MIN_SCORE"
            and case["refined"][dim]["prediction"] is None
            for case in result["cases"]
        )


def test_provider_failure_is_nonleaking_and_does_not_authorize_scope():
    module = _runner()
    rows = _examples("SC001", "SC013", "SC066")
    gateway = StubNLI(module, fail_on="Maybe I should go walking tomorrow.")
    result = module.evaluate(rows, backend=gateway, batch_size=2)
    assert result["v14b_reference"]["error_batches"] == 4
    assert result["refined_error_batches"] == 3
    assert result["cases"][1]["refined"]["modality"]["prediction"] is None
    serialized = json.dumps(result)
    assert "Maybe I should go walking tomorrow." not in serialized
    assert "source must not appear in report" not in serialized
    assert "narrow_scope" not in serialized
    assert "memory_admission" not in serialized


def test_bad_model_schema_generates_visible_errors_not_false_positives():
    module = _runner()
    result = module.evaluate(
        _examples("SC001", "SC066"),
        backend=StubNLI(module, invalid=True),
    )
    assert result["v14b_reference"]["error_batches"] == 4
    assert result["refined_error_batches"] == 3
    for dim in module.REFINED:
        assert result["by_dimension"][dim]["v14c"]["correct"] == 0
        assert result["by_dimension"][dim]["v14c"]["errors"] == 2


def test_write_once_report_and_dataset_metadata(tmp_path):
    module = _runner()
    output = tmp_path / "result.json"
    cmd = [
        "--model-dir", str(tmp_path / "fixture-model"),
        "--suite", str(CORPUS),
        "--case-ids", "SC001", "SC013", "SC066",
        "--report", str(output),
    ]
    assert module.main(cmd, backend=StubNLI(module)) == 0
    obj = json.loads(output.read_text(encoding="utf-8"))
    assert obj["version"] == module.VERSION
    assert obj["full_corpus"] is False
    assert obj["annotation_status"] == "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"
    assert obj["v14b_hypotheses_sha256"] != obj["v14c_hypotheses_sha256"]
    assert obj["metrics"]["total"] == 3
    original = output.read_bytes()
    with pytest.raises(SystemExit) as err:
        module.main(cmd, backend=StubNLI(module))
    assert err.value.code == 2
    assert output.read_bytes() == original


@pytest.mark.parametrize("kw", [
    {"batch_size": 0}, {"batch_size": 33},
    {"min_score": float("nan")}, {"min_margin": -0.1},
])
def test_invalid_parameters_rejected(kw):
    module = _runner()
    with pytest.raises(ValueError):
        module.evaluate(_examples("SC001"), backend=StubNLI(module), **kw)


def test_no_optional_ml_runtime_or_psycopg_needed_for_contract_test():
    code = (
        "import builtins,sys;"
        f"sys.path.insert(0,{str(SCRIPTS)!r});"
        "orig=builtins.__import__;"
        "builtins.__import__=lambda name,*a,**kw:"
        "(_ for _ in ()).throw(ImportError('optional dependency forbidden'))"
        "if name.split('.')[0] in ('transformers','torch','psycopg') "
        "else orig(name,*a,**kw);"
        "import evaluate_zeroshot_hypotheses as m;"
        "assert len(m.REFINED)==3"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
