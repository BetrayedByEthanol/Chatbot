"""MVP-02C.14F benchmarking contracts; injected models are NOT real benchmarks."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "scenarios" / "mvp02c14d_semantic_drafts.json"
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
mod = importlib.import_module("benchmark_semantic_models")
evalmod = importlib.import_module("evaluate_semantic_models")


class FixedProbe:
    backend_id = "test.fixed.realistic.response"

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def predict(self, *, source, quote):
        self.calls.append((source, quote))
        if self.fail:
            raise RuntimeError("source leak forbidden " + source)
        return evalmod.ProbeLabels.from_mapping({
            "statement_type": "PREFERENCE",
            "certainty": "ASSERTED", "polarity": "POSITIVE",
            "habituality": None, "conditionality": False,
            "hypotheticality": False,
            "temporal_references": ["PRESENT"],
        })


def test_benchmark_warmup_and_repetitions_are_nonleaking_and_paired():
    backend = FixedProbe()
    report, profile = mod.run_benchmark(
        suite=SUITE, backend=backend, backend_name="spacy",
        case_ids=("SD001", "SD002"),
        repetitions=3, warmups=2,
        construction_ms=2.0,
    )
    assert len(backend.calls) == 2 + 3 * 3
    assert backend.calls[0] == backend.calls[1] == (
        "The lights are on.", "The lights are on")
    assert report["sample_cases"] == profile["sample_cases"] == 2
    assert report["sample_claims"] == profile["sample_claims"] == 3
    assert profile["case_ids"] == ["SD001", "SD002"]
    assert profile["construction_ms"] == 2.0
    assert profile["warmup"]["count"] == 2
    assert profile["warmup"]["errors"] == 0
    assert len(profile["runs"]) == 3
    assert profile["runs"][0]["by_dimension"] == report["by_dimension"]
    assert profile["timing"]["p95_suite_ms"] >= profile["timing"]["median_suite_ms"]
    assert profile["model"]["trained_pipeline"] is False
    payload = json.dumps(profile)
    assert "I enjoy gardening" not in payload
    assert "I used to enjoy" not in payload


def test_benchmark_invalid_outputs_are_measured_as_errors_not_truth():
    backend = FixedProbe(fail=True)
    report, profile = mod.run_benchmark(
        suite=SUITE, backend=backend, backend_name="spacy",
        case_ids=("SD001",), repetitions=2, warmups=1,
    )
    assert profile["warmup"]["errors"] == 1
    assert report["error_claims"] == 1
    assert profile["runs"][0]["error_claims"] == 1
    assert profile["runs"][1]["error_claims"] == 1
    assert report["by_dimension"]["statement_type"]["abstained"] == 1
    assert "source leak forbidden" not in json.dumps(profile)


@pytest.mark.parametrize("repeat,warmup", [(0, 1), (11, 1), (1, -1), (1, 6)])
def test_benchmark_bounded_repetitions(repeat, warmup):
    with pytest.raises(ValueError):
        mod.run_benchmark(
            suite=SUITE, backend=FixedProbe(), backend_name="spacy",
            case_ids=("SD001",), repetitions=repeat, warmups=warmup,
        )


def test_ollama_provenance_uses_verified_local_digest():
    digest = "a" * 64
    tags = json.dumps({"models": [
        {"name": "qwen2.5:0.5b", "digest": digest, "size": 430000000},
    ]}).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return tags

    class FakeOpener:
        def open(self, req, timeout):
            assert req.full_url == "http://127.0.0.1:11434/api/tags"
            assert timeout == 10
            return FakeResponse()

    instance = SimpleNamespace(
        url="http://127.0.0.1:11434", _opener=FakeOpener()
    )
    provenance = mod._model_provenance(
        backend_name="ollama", backend=instance,
        model_dir=None, model_name="qwen2.5:0.5b",
    )
    assert provenance["digest"] == digest
    assert provenance["digest_verified"] is True


def test_ollama_provenance_rejects_model_alias_without_digest():
    tags = json.dumps({"models": [
        {"name": "qwen2.5:0.5b", "digest": "mutable"},
    ]}).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return tags

    instance = SimpleNamespace(
        url="http://127.0.0.1:11434",
        _opener=SimpleNamespace(open=lambda *_args, **_kw: FakeResponse()),
    )
    with pytest.raises(ValueError, match="digest"):
        mod._model_provenance(
            backend_name="ollama", backend=instance,
            model_dir=None, model_name="qwen2.5:0.5b",
        )


def test_cli_produces_write_once_v14e_report_and_v14f_profile(tmp_path):
    outputs = [tmp_path / "scores.json", tmp_path / "profile.json"]
    args = [
        "--backend", "spacy",
        "--case-ids", "SD001", "SD007",
        "--repetitions", "2", "--warmups", "1",
        "--report", str(outputs[0]), "--profile", str(outputs[1]),
    ]
    assert mod.main(args, injected_backend=FixedProbe()) == 0
    written = [path.read_text(encoding="utf-8") for path in outputs]
    assert json.loads(written[0])["version"] == evalmod.VERSION
    assert json.loads(written[1])["version"] == mod.VERSION
    assert json.loads(written[1])["model"]["trained_pipeline"] is False
    assert "I enjoy gardening" not in written[1]
    with pytest.raises(SystemExit):
        mod.main(args, injected_backend=FixedProbe())
    assert [path.read_text(encoding="utf-8") for path in outputs] == written


def test_profile_does_not_claim_ollama_process_rss_or_model_accuracy():
    _, profile = mod.run_benchmark(
        suite=SUITE, backend=FixedProbe(), backend_name="spacy",
        case_ids=("SD001",), repetitions=1, warmups=0,
    )
    assert any("Ollama server" in text for text in profile["limits"])
    assert any("not independently reviewed" in text for text in profile["limits"])
    assert not any("accuracy" in key for key in profile)
