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


class StubLoadedTokenizer:
    """Models the actual tokenizer serialization boundary without HF weights."""

    def __init__(self, config=b'{"do_lower_case":true}'):
        self.config = config

    def save_pretrained(self, destination: str):
        folder = Path(destination)
        (folder / "tokenizer.json").write_bytes(b'{"model":{"type":"WordPiece"}}')
        (folder / "tokenizer_config.json").write_bytes(self.config)


class FixedProbe:
    backend_id = "test.fixed.realistic.response"

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail
        self.client = SimpleNamespace(
            _pipeline=SimpleNamespace(tokenizer=StubLoadedTokenizer())
        )

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
    assert profile["nli_decision_thresholds"] is None
    payload = json.dumps(profile)
    assert "I enjoy gardening" not in payload
    assert "I used to enjoy" not in payload


def _fake_deberta_checkpoint(root: Path) -> Path:
    root.mkdir()
    (root / "config.json").write_text('{"model_type":"deberta-v2"}', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"mock-only-not-a-real-model")
    (root / "tokenizer.json").write_bytes(b'{"vocab":{"hello":0}}')
    (root / "tokenizer_config.json").write_text(
        '{"do_lower_case":true}', encoding="utf-8"
    )
    (root / "special_tokens_map.json").write_text(
        '{"unk_token":"[UNK]"}', encoding="utf-8"
    )
    return root


def test_nondefault_effective_deberta_thresholds_are_archived(tmp_path):
    backend = FixedProbe()
    backend.min_score = .87
    backend.min_margin = .23
    model_dir = _fake_deberta_checkpoint(tmp_path / "mock-deberta")
    report, profile = mod.run_benchmark(
        suite=SUITE, backend=backend, backend_name="deberta",
        case_ids=("SD001",), repetitions=2, warmups=0, model_dir=model_dir,
    )
    assert report["sample_claims"] == 1
    assert profile["nli_decision_thresholds"] == {
        "min_score": .87, "min_margin": .23,
    }
    assert profile["model"]["weights_sha256"]
    tokenizer = profile["model"]["tokenizer"]
    assert tokenizer["source_file_sha256"]["tokenizer.json"]
    assert tokenizer["source_file_sha256"]["tokenizer_config.json"]
    assert tokenizer["source_file_sha256"]["special_tokens_map.json"]
    assert tokenizer["effective_file_sha256"]["tokenizer_config.json"]
    assert tokenizer["effective_tokenizer_sha256"]
    assert "I enjoy gardening" not in json.dumps(profile)



def test_tokenizer_artifact_changes_produce_different_checkpoint_fingerprints(tmp_path):
    model_dir = _fake_deberta_checkpoint(tmp_path / "deberta")
    backend = FixedProbe()
    args = dict(
        backend_name="deberta", backend=backend,
        model_dir=model_dir, model_name=None,
    )
    first = mod._model_provenance(**args)
    # Model config, native weights, and loaded tokenizer are unchanged.
    (model_dir / "tokenizer.json").write_bytes(b'{"vocab":{"hello":1}}')
    second = mod._model_provenance(**args)
    assert first["weights_sha256"] == second["weights_sha256"]
    assert first["model_config_sha256"] == second["model_config_sha256"]
    assert (first["tokenizer"]["source_file_sha256"]["tokenizer.json"]
            != second["tokenizer"]["source_file_sha256"]["tokenizer.json"])
    assert (first["tokenizer"]["effective_tokenizer_sha256"]
            == second["tokenizer"]["effective_tokenizer_sha256"])


def test_tokenizer_settings_and_loaded_snapshot_change_provenance(tmp_path):
    model_dir = _fake_deberta_checkpoint(tmp_path / "deberta")
    backend = FixedProbe()
    args = dict(
        backend_name="deberta", backend=backend,
        model_dir=model_dir, model_name=None,
    )
    original = mod._model_provenance(**args)
    (model_dir / "tokenizer_config.json").write_text(
        '{"do_lower_case":false}', encoding="utf-8"
    )
    (model_dir / "special_tokens_map.json").write_text(
        '{"unk_token":"[UNKNOWN]"}', encoding="utf-8"
    )
    backend.client._pipeline.tokenizer.config = b'{"do_lower_case":false}'
    changed = mod._model_provenance(**args)
    assert original["weights_sha256"] == changed["weights_sha256"]
    assert original["model_config_sha256"] == changed["model_config_sha256"]
    for name in ("tokenizer_config.json", "special_tokens_map.json"):
        assert (original["tokenizer"]["source_file_sha256"][name]
                != changed["tokenizer"]["source_file_sha256"][name])
    assert (original["tokenizer"]["effective_tokenizer_sha256"]
            != changed["tokenizer"]["effective_tokenizer_sha256"])


def test_deberta_provenance_requires_real_tokenizer_inputs_and_loaded_state(tmp_path):
    model_dir = _fake_deberta_checkpoint(tmp_path / "deberta")
    backend = FixedProbe()
    (model_dir / "tokenizer.json").unlink()
    args = dict(
        backend_name="deberta", backend=backend,
        model_dir=model_dir, model_name=None,
    )
    with pytest.raises(ValueError, match="tokenizer inputs"):
        mod._model_provenance(**args)
    (model_dir / "spm.model").write_bytes(b"test-sentencepiece-file")
    assert mod._model_provenance(**args)["tokenizer"]["source_file_sha256"]["spm.model"]
    backend.client._pipeline.tokenizer = object()
    with pytest.raises(ValueError, match="loaded Transformers tokenizer"):
        mod._model_provenance(**args)


def test_cli_records_backend_thresholds_not_requested_cli_values(tmp_path):
    backend = FixedProbe()
    backend.min_score = .91
    backend.min_margin = .42
    checkpoint = _fake_deberta_checkpoint(tmp_path / "mock-deberta")
    result = tmp_path / "score.json"
    profile = tmp_path / "profile.json"
    args = [
        "--backend", "deberta", "--model-dir", str(checkpoint),
        "--min-score", "0.21", "--min-margin", "0.04",
        "--case-ids", "SD001", "--repetitions", "1", "--warmups", "0",
        "--report", str(result), "--profile", str(profile),
    ]
    assert mod.main(args, injected_backend=backend) == 0
    saved = json.loads(profile.read_text(encoding="utf-8"))
    assert saved["nli_decision_thresholds"] == {
        "min_score": .91, "min_margin": .42,
    }
    assert json.loads(result.read_text(encoding="utf-8"))["sample_claims"] == 1


@pytest.mark.parametrize("key,value", [
    ("min_score", None), ("min_score", -0.1), ("min_score", float("nan")),
    ("min_margin", 1.1), ("min_margin", "0.25"), ("min_margin", True),
])
def test_deberta_missing_or_invalid_active_thresholds_fail_closed(
    tmp_path, key, value,
):
    backend = FixedProbe()
    backend.min_score = .65
    backend.min_margin = .10
    setattr(backend, key, value)
    model_dir = _fake_deberta_checkpoint(tmp_path / "mock-deberta")
    with pytest.raises(ValueError, match="valid NLI thresholds"):
        mod.run_benchmark(
            suite=SUITE, backend=backend, backend_name="deberta",
            case_ids=("SD001",), repetitions=1, warmups=0,
            model_dir=model_dir,
        )


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
