"""MVP-02C.14I: offline prompt comparison and no-promotion contracts."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

mod = importlib.import_module("evaluate_semantic_prompt_quality")
probe = importlib.import_module("evaluate_semantic_models")
SUITE = ROOT / "scenarios" / "mvp02c14d_semantic_drafts.json"


def test_default_prompt_identical_to_legacy_text_and_candidate_versioned():
    expected = (
        "Classify the TARGET CLAIM in the context of the FULL MESSAGE. "
        "Output ONLY JSON with exactly: statement_type "
        "(FACT, PREFERENCE, EXPERIENCE, INTENTION, OPINION, UNKNOWN), "
        "certainty (ASSERTED, TENTATIVE, UNKNOWN), polarity "
        "(POSITIVE, NEGATIVE, UNKNOWN), habituality, conditionality, "
        "hypotheticality (each true, false or null), temporal_references "
        "(array of zero or more of PAST, PRESENT, FUTURE). "
        "Use null or UNKNOWN if uncertain. A present intention can mention "
        "future time; a change in a preference does not necessarily correct "
        "the prior conversation. Never invent missing source text."
    )
    assert probe._ollama_instruction("baseline") == expected
    assert probe.OLLAMA_PROMPT_VERSIONS["baseline"] == probe.PROMPT_VERSION
    candidate = probe._ollama_instruction("semantic_scope_v1")
    assert candidate.startswith(expected)
    assert candidate != expected
    assert "PREFERENCE" in candidate and "PAST" in candidate
    assert "SD001" not in candidate
    with pytest.raises(ValueError, match="unsupported"):
        probe._ollama_instruction("another")


def test_constructor_defaults_to_old_wire_format_and_variant_is_opt_in(monkeypatch):
    sent = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return json.dumps({"message": {"content": json.dumps({
                "statement_type": "UNKNOWN", "certainty": "UNKNOWN",
                "polarity": "UNKNOWN", "habituality": None,
                "conditionality": None, "hypotheticality": None,
                "temporal_references": [],
            })}}).encode()

    class Opener:
        def open(self, request, *, timeout):
            sent.append(json.loads(request.data))
            return Response()

    monkeypatch.setattr(probe, "build_opener", lambda _: Opener())
    default = probe.OllamaJsonProbe(model="qwen2.5:0.5b")
    default.predict(source="PRIVATE_EXAMPLE", quote="PRIVATE_EXAMPLE")
    candidate = probe.OllamaJsonProbe(
        model="qwen2.5:0.5b", format_mode="schema",
        prompt_variant="semantic_scope_v1",
    )
    candidate.predict(source="PRIVATE_EXAMPLE", quote="PRIVATE_EXAMPLE")
    assert sent[0]["format"] == "json"
    assert sent[0]["messages"][0]["content"] == probe._ollama_instruction("baseline")
    assert sent[1]["format"]["additionalProperties"] is False
    assert sent[1]["messages"][0]["content"] == probe._ollama_instruction(
        "semantic_scope_v1"
    )
    assert sent[0]["options"] == sent[1]["options"] == {
        "temperature": 0, "num_predict": 350,
    }
    assert sent[0]["messages"][1] == sent[1]["messages"][1]
    assert "PRIVATE_EXAMPLE" not in probe._ollama_instruction("semantic_scope_v1")
    with pytest.raises(ValueError, match="unsupported"):
        probe.OllamaJsonProbe(model="qwen2.5:0.5b", prompt_variant="unapproved")


CALLS = []


class FixedQualityProbe:
    backend_id = "ollama.local_json.qwen2.5:0.5b"

    def __init__(self, *, prompt_variant, format_mode, diagnostic_sink, **kwargs):
        assert format_mode == "schema"
        self.variant = prompt_variant
        self.sink = diagnostic_sink
        CALLS.append(prompt_variant)

    def predict(self, *, source, quote):
        self.sink("VALID_PREDICTION")
        return probe.ProbeLabels.from_mapping({
            "statement_type": ("UNKNOWN" if self.variant == "baseline"
                               else "PREFERENCE"),
            "certainty": "UNKNOWN", "polarity": "UNKNOWN",
            "habituality": None, "conditionality": None,
            "hypotheticality": None, "temporal_references": [],
        })


def _fake_fingerprint(**_):
    return {"digest_verified": True, "digest": "b" * 64,
            "model_name": "qwen2.5:0.5b"}


def _args():
    return dict(
        model="qwen2.5:0.5b", endpoint="http://127.0.0.1:11434",
        timeout_s=60, suite=SUITE, case_ids=("SD001", "SD002"),
        backend_factory=FixedQualityProbe,
        provenance_resolver=_fake_fingerprint,
    )


def test_interleaved_repetitions_same_reference_and_no_approval():
    CALLS.clear()
    report = mod.run_experiment(**_args(), repetitions=2)
    assert CALLS == ["baseline", "semantic_scope_v1"] * 2
    assert report["case_ids"] == ["SD001", "SD002"]
    assert report["sample_cases"] == 2 and report["sample_claims"] == 3
    assert report["format_mode"] == "schema"
    assert report["decision"] == "NO_PROMPT_APPROVED_FOR_PRODUCTION"
    assert report["experiment_order"] == "INTERLEAVED_BASELINE_CANDIDATE"
    a, b = report["candidates"]
    assert a["prompt_version"] != b["prompt_version"]
    assert a["prompt_sha256"] != b["prompt_sha256"]
    assert all(r["valid_prediction_claims"] == 3 for item in (a, b)
               for r in item["repetitions"])
    assert a["repetitions"][0]["by_dimension"]["statement_type"]["supported"] == 0
    assert b["repetitions"][0]["by_dimension"]["statement_type"]["supported"] == 3
    assert report["aggregate_differences_by_repetition"][0][
        "by_dimension"]["statement_type"]["supported_difference"] == 3
    assert "I enjoy gardening" not in json.dumps(report)
    assert "SECRET" not in json.dumps(report)


def test_repetitions_bounded_and_report_write_once(tmp_path):
    for repeats in (0, 4, True):
        with pytest.raises(ValueError, match="1..3"):
            mod.run_experiment(**_args(), repetitions=repeats)
    output = tmp_path / "quality.json"
    argv = ["--model", "qwen2.5:0.5b", "--case-ids", "SD001",
            "--repetitions", "1", "--report", str(output)]
    assert mod.main(argv, backend_factory=FixedQualityProbe,
                    provenance_resolver=_fake_fingerprint) == 0
    saved = output.read_bytes()
    assert json.loads(saved)["sample_claims"] == 1
    with pytest.raises(SystemExit):
        mod.main(argv, backend_factory=FixedQualityProbe,
                 provenance_resolver=_fake_fingerprint)
    assert output.read_bytes() == saved


def test_invalid_aggregates_and_provenance_fail_closed(monkeypatch):
    original = mod._run_mode

    def inconsistent(*, factory, **kwargs):
        outcome, backend = original(factory=factory, **kwargs)
        outcome["by_dimension"]["statement_type"]["eligible"] += 1
        return outcome, backend

    monkeypatch.setattr(mod, "_run_mode", inconsistent)
    with pytest.raises(ValueError, match="inconsistent dimension scores"):
        mod.run_experiment(**_args(), repetitions=1)


def test_model_digest_drift_across_prompt_variants_fails_closed():
    values = iter(("b" * 64, "c" * 64))

    def unstable(**_):
        return {"digest_verified": True, "digest": next(values)}
    args = _args()
    args["provenance_resolver"] = unstable
    with pytest.raises(ValueError, match="provenance changed"):
        mod.run_experiment(**args, repetitions=1)


def test_invalid_failure_histogram_rejected():
    result = {
        "sample_claims": 1, "valid_prediction_claims": 0, "error_claims": 1,
        "failure_histogram": {"INVENTED": 2},
        "by_dimension": {field: {
            "eligible": 1, "supported": 0, "correct": 0, "incorrect": 0,
            "abstained": 1, "unscored_reference": 0,
        } for field in probe.LABEL_FIELDS},
    }
    with pytest.raises(ValueError, match="failure histogram"):
        mod._validate_run(result)
