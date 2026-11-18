"""Contract tests for MVP-02C.14B zero-shot diagnostic (no model required)."""
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


def _module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "offline_zeroshot_test", SCRIPTS / "evaluate_zeroshot_semantics.py"
    )
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def _selected(*ids):
    examples = json.loads(CORPUS.read_text(encoding="utf-8"))["examples"]
    return [example for example in examples if example["id"] in ids]


class ScriptedNLI:
    """Intentionally fake model: verify harness contracts, NOT model ability."""

    def __init__(self, module, *, fail_text=None, malformed=None):
        self.module = module
        self.calls = []
        self.fail_text = fail_text
        self.malformed = malformed
        self.script = {
            "I enjoy gardening.": (
                "PREFERENCE", "CURRENT", "ASSERTED", "INFORMING"
            ),
            "Maybe I should go walking tomorrow.": (
                "INTENTION", "FUTURE", "TENTATIVE", "INFORMING"
            ),
            "Actually, I don't enjoy gardening anymore.": (
                "PREFERENCE", "CURRENT", "ASSERTED", "CORRECTING"
            ),
        }

    def classify(self, texts, hypotheses):
        self.calls.append((list(texts), list(hypotheses)))
        if self.fail_text and self.fail_text in texts:
            raise RuntimeError("test server failure (must not leak)")
        if self.malformed is not None:
            return [self.malformed for _ in texts]
        dimensions = list(self.module.HYPOTHESES)
        kind = next(
            key for key in dimensions
            if set(hypotheses) == set(self.module.HYPOTHESES[key].values())
        )
        idx = dimensions.index(kind)
        results = []
        for text in texts:
            selected_label = self.script[text][idx]
            mapping = self.module.HYPOTHESES[kind]
            # Reverse the HF output order to detect index-based parsing.
            labels = list(reversed(hypotheses))
            values = [0.92 if label == mapping[selected_label] else 0.08
                      for label in labels]
            results.append({"sequence": text, "labels": labels, "scores": values})
        return results


def test_all_eighty_existing_examples_and_draft_status_preserved():
    _module()  # Ensure the scripts directory is importable in CI.
    from evaluate_semantic_scope import load_corpus, LABEL_STATUS
    rows, digest = load_corpus(CORPUS)
    assert len(rows) == 80
    assert len(digest) == 64
    assert LABEL_STATUS == "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"
    module = _module()
    assert set(module.HYPOTHESES) == {
        "statement_type", "temporal_scope", "modality", "pragmatic_function"
    }
    assert len(module.HYPOTHESES["temporal_scope"]) == 6
    assert len(module.HYPOTHESES["modality"]) == 5


def test_injected_nli_scores_all_dimensions_and_compares_existing_stage_a():
    module = _module()
    rows = _selected("SC001", "SC013", "SC066")
    gateway = ScriptedNLI(module)
    report = module.evaluate(rows, backend=gateway, batch_size=2)
    assert report["total"] == 3
    assert report["batch_calls"] == 8
    assert report["error_batches"] == 0
    assert len(gateway.calls) == 8
    assert report["by_dimension"]["statement_type"]["baseline"]["supported"] == 2
    for dimension, result in report["by_dimension"].items():
        assert result["zero_shot"]["supported"] == 3, dimension
        assert result["zero_shot"]["correct"] == 3, dimension
        assert result["zero_shot"]["abstained"] == 0, dimension
    assert report["by_dimension"]["pragmatic_function"]["baseline"]["supported"] == 2
    assert report["cases"][-1]["zero_shot"]["pragmatic_function"]["prediction"] == "CORRECTING"
    assert report["cases"][1]["zero_shot"]["modality"]["prediction"] == "TENTATIVE"
    # Only the premise texts and hypothesis strings reached the model;
    # no draft labels or private corpus identifiers were passed.
    assert all(isinstance(text, str) for batch in gateway.calls for text in batch[0])
    assert all("SC001" not in text for batch in gateway.calls for text in batch[0])


def test_multilabel_entailment_scores_stay_distinct_and_not_probabilities():
    module = _module()
    gateway = ScriptedNLI(module)
    report = module.evaluate(_selected("SC001"), backend=gateway)
    predictions = report["cases"][0]["zero_shot"]["statement_type"]
    assert predictions["status"] == "PREDICTED"
    assert predictions["scores"]["PREFERENCE"] == 0.92
    assert sum(predictions["scores"].values()) != pytest.approx(1.0)


def test_configurable_abstentions_are_not_counted_as_correct():
    module = _module()
    report = module.evaluate(
        _selected("SC001", "SC013"), backend=ScriptedNLI(module),
        min_score=0.95,
    )
    for dimension, counts in report["by_dimension"].items():
        m = counts["zero_shot"]
        assert m["supported"] == m["correct"] == m["incorrect"] == 0
        assert m["abstained"] == 2
        assert all(
            c["zero_shot"][dimension]["prediction"] is None
            for c in report["cases"]
        )
    # Same score threshold and a stricter margin are different reasons.
    runner_up = module.evaluate(
        _selected("SC001"), backend=ScriptedNLI(module),
        min_margin=0.95,
    )
    assert runner_up["cases"][0]["zero_shot"]["modality"]["reason"] == "BELOW_MIN_MARGIN"


@pytest.mark.parametrize("bad", [
    {"labels": [], "scores": []},
    {"labels": ["one"], "scores": [0.9]},
    {"labels": [1], "scores": [0.9]},
    {"labels": ["missing"], "scores": [float("nan")]},
    {"labels": ["missing"], "scores": [1.1]},
    {"scores": [0.9]},
    {"labels": ["missing"]},
])
def test_bad_output_is_detected_without_imputing_missing_labels(bad):
    module = _module()
    report = module.evaluate(
        _selected("SC001", "SC013"), backend=ScriptedNLI(module, malformed=bad),
    )
    assert report["error_batches"] == 4
    assert all(
        dim["zero_shot"]["supported"] == 0
        and dim["zero_shot"]["errors"] == 2
        for dim in report["by_dimension"].values()
    )
    assert all(
        c["zero_shot"]["statement_type"]["prediction"] is None
        for c in report["cases"]
    )


def test_provider_failure_does_not_abort_other_dimensions_or_persist_source():
    module = _module()
    report = module.evaluate(
        _selected("SC001", "SC013", "SC066"),
        backend=ScriptedNLI(module, fail_text="Maybe I should go walking tomorrow."),
        batch_size=2,
    )
    assert report["error_batches"] == 4
    assert all(
        r["zero_shot"]["statement_type"]["status"] == "MODEL_ERROR"
        for r in report["cases"][:2]
    )
    assert report["cases"][2]["zero_shot"]["statement_type"]["status"] == "PREDICTED"
    serialized = json.dumps(report)
    assert "I enjoy gardening." not in serialized
    assert "Maybe I should go walking tomorrow." not in serialized
    assert "test server failure" not in serialized


@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0}, {"batch_size": 33},
    {"min_score": -0.01}, {"min_score": float("nan")},
    {"min_score": float("inf")}, {"min_margin": 1.1},
])
def test_invalid_eval_parameters_are_rejected(kwargs):
    module = _module()
    with pytest.raises(ValueError):
        module.evaluate(
            _selected("SC001"), backend=ScriptedNLI(module), **kwargs
        )


def test_cli_write_once_and_subset_with_scripted_backend(tmp_path):
    module = _module()
    path = tmp_path / "test-result.json"
    backend = ScriptedNLI(module)
    args = [
        "--model-dir", str(tmp_path / "pretend-only-in-injected-test"),
        "--suite", str(CORPUS), "--report", str(path),
        "--case-ids", "SC001", "SC013", "SC066",
        "--batch-size", "2",
    ]
    assert module.main(args, backend=backend) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["version"] == module.VERSION
    assert report["annotation_status"] == "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"
    assert report["full_corpus"] is False
    assert report["metrics"]["total"] == 3
    assert report["hypotheses_sha256"]
    first = path.read_bytes()
    with pytest.raises(SystemExit) as err:
        module.main(args, backend=backend)
    assert err.value.code == 2
    assert path.read_bytes() == first


def test_bad_case_id_rejected_before_inference(tmp_path):
    module = _module()
    gateway = ScriptedNLI(module)
    with pytest.raises(SystemExit) as err:
        module.main([
            "--model-dir", str(tmp_path / "not-used"),
            "--suite", str(CORPUS), "--report", str(tmp_path / "bad.json"),
            "--case-ids", "SC001", "SC001",
        ], backend=gateway)
    assert err.value.code == 2
    assert gateway.calls == []


def test_no_psycopg_or_transformers_required_to_import_harness():
    command = (
        "import builtins,sys;"
        f"sys.path.insert(0,{str(SCRIPTS)!r});"
        "old=builtins.__import__;"
        "builtins.__import__=lambda name,*args,**kwargs:"
        "(_ for _ in ()).throw(ImportError('forbidden optional'))"
        "if name.split('.')[0] in ('psycopg','torch','transformers') "
        "else old(name,*args,**kwargs);"
        "import evaluate_zeroshot_semantics;"
        "assert len(evaluate_zeroshot_semantics.HYPOTHESES)==4"
    )
    r = subprocess.run(
        [sys.executable, "-c", command], text=True,
        capture_output=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr


def test_local_adapter_validates_directory_without_loading_hf(tmp_path):
    module = _module()
    with pytest.raises((ValueError, FileNotFoundError)):
        module.LocalTransformersZeroShot(tmp_path, device="cpu")
