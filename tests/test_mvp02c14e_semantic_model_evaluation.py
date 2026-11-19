"""MVP-02C.14E — injected-backend contracts, no model downloads in CI.

Measurements against author drafts are diagnostics, NOT real semantic accuracy
or end-to-end claim extraction capability.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_semantic_models.py"
SUITE = ROOT / "scenarios" / "mvp02c14d_semantic_drafts.json"


def _module():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("mvp02c14e_eval", SCRIPT)
    instance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(instance)
    return instance


class ScriptedBackend:
    backend_id = "test.scripted"

    def __init__(self, module, *, error_quote=None):
        self.module = module
        self.error_quote = error_quote
        self.calls = []

    def predict(self, *, source, quote):
        self.calls.append((source, quote))
        if quote == self.error_quote:
            raise RuntimeError("PRIVATE SOURCE IN ERROR: " + source)
        return self.module.ProbeLabels.from_mapping({
            "statement_type": "PREFERENCE",
            "certainty": "ASSERTED",
            "polarity": "POSITIVE",
            "habituality": None,
            "conditionality": False,
            "hypotheticality": False,
            "temporal_references": ["PRESENT"],
        })


def test_version_frozen_reference_spans_not_labels_sent_to_backend():
    m = _module()
    backend = ScriptedBackend(m)
    report = m.evaluate_probe(
        SUITE, backend=backend, case_ids=("SD001", "SD002"),
    )
    assert report["version"] == m.VERSION
    assert report["reference_mode"] == "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY"
    assert report["annotation_status"] == "AUTHOR_DRAFT_NOT_INDEPENDENT"
    assert report["sample_cases"] == 2
    assert report["sample_claims"] == len(backend.calls) == 3
    assert backend.calls[0] == ("I enjoy gardening.", "I enjoy gardening")
    assert backend.calls[1][0] == backend.calls[2][0]
    assert backend.calls[1][1] != backend.calls[2][1]
    assert report["by_dimension"]["statement_type"] == {
        "eligible": 3, "supported": 3, "correct": 3,
        "incorrect": 0, "abstained": 0, "unscored_reference": 0,
    }
    assert report["by_temporal_label"]["PAST"]["fn"] == 1
    assert report["by_temporal_label"]["PRESENT"]["tp"] == 2
    assert "I enjoy gardening" not in json.dumps(report)
    assert "but now" not in json.dumps(report)


def test_missing_predictions_and_errors_abstain_without_leaking_text():
    m = _module()
    backend = ScriptedBackend(m, error_quote="I enjoy gardening")
    report = m.evaluate_probe(
        SUITE, backend=backend, case_ids=("SD001", "SD004"),
    )
    assert report["error_claims"] == 1
    assert report["by_dimension"]["statement_type"]["abstained"] == 1
    assert report["by_dimension"]["temporal_references"]["abstained"] == 1
    assert report["by_temporal_label"]["PAST"]["abstained"] == 1
    assert "PRIVATE SOURCE IN ERROR" not in json.dumps(report)
    assert "I enjoy gardening" not in json.dumps(report)


def test_probe_labels_validate_all_fields_strictly_without_conversion_errors():
    m = _module()
    values = dict(
        statement_type="INTENTION", certainty="ASSERTED", polarity="POSITIVE",
        habituality=None, conditionality=False, hypotheticality=False,
        temporal_references=["PRESENT", "FUTURE"],
    )
    out = m.ProbeLabels.from_mapping(values)
    assert out.temporal_references == ("PRESENT", "FUTURE")
    for update in (
        {"temporal_references": ["PAST", "PAST"]},
        {"temporal_references": ["YESTERDAY"]},
        {"conditionality": "false"},
        {"statement_type": "FANCY"},
        {"certainty": "MAYBE"},
        {"polarity": "LIKES"},
        {"bogus": "value"},
    ):
        with pytest.raises((ValueError, Exception)):
            m.ProbeLabels.from_mapping({**values, **update})
    with pytest.raises(ValueError):
        m.ProbeLabels.from_mapping({"statement_type": "FACT"})


def test_full_suite_selection_must_be_exact_and_provisional():
    m = _module()
    backend = ScriptedBackend(m)
    with pytest.raises(ValueError, match="duplicate or empty"):
        m.evaluate_probe(
            SUITE, backend=backend, case_ids=("SD001", "SD001"),
        )
    with pytest.raises(ValueError, match="unknown case"):
        m.evaluate_probe(SUITE, backend=backend, case_ids=("UNKNOWN_ID",))
    assert backend.calls == []


def test_structural_spacy_baseline_uses_no_other_claims(monkeypatch):
    m = _module()

    class Token:
        def __init__(self, word):
            self.lower_ = word.casefold()

    fake = SimpleNamespace(blank=lambda language: (
        lambda text: [Token(word) for word in text.replace(".", "").split()]
    ))
    monkeypatch.setitem(sys.modules, "spacy", fake)
    probe = m.SpacyStructuralProbe()
    example = probe.predict(
        source="Yesterday I ran; tomorrow I might go walking.",
        quote="I might go walking",
    )
    assert example.certainty == "TENTATIVE"
    assert example.temporal_references == ()
    assert example.statement_type == "UNKNOWN"
    assert example.habituality is None
    habitual = probe.predict(
        source="I walk every weekend.", quote="I walk every weekend",
    )
    assert habitual.habituality is True


def test_hf_nli_injected_scores_keep_binary_and_temporal_abstentions(monkeypatch):
    m = _module()
    # Avoid loading Transformers, but exercise actual frozen probe hypotheses,
    # raw-NLI score parsing and threshold logic from the reused v14B evaluator.
    from evaluate_zeroshot_semantics import _select

    class FakeNli:
        def __init__(self):
            self.calls = []

        def classify(self, texts, hypotheses):
            self.calls.append((texts, hypotheses))
            # Select only binary FALSE and positive PREFERENCE with high score.
            chosen = (
                "The target claim expresses a preference."
                if "The target claim expresses a preference." in hypotheses
                else "The target claim is presented as asserted."
                if "The target claim is presented as asserted." in hypotheses
                else hypotheses[-1]
            )
            return [{
                "labels": list(reversed(hypotheses)),
                "scores": [0.9 if h == chosen else 0.1
                           for h in reversed(hypotheses)],
            }]

    probe = object.__new__(m.DebertaNliProbe)
    probe.client = FakeNli()
    probe._select = _select
    probe.min_score = 0.6
    probe.min_margin = 0.2
    claim = probe.predict(
        source="I enjoy gardening.", quote="I enjoy gardening",
    )
    assert claim.statement_type == "PREFERENCE"
    assert claim.certainty == "ASSERTED"
    assert claim.temporal_references == ()
    assert len(probe.client.calls) == len(m.HYPOTHESES)
    for texts, _hypotheses in probe.client.calls:
        assert texts == ["Full message: I enjoy gardening.\nTarget claim: I enjoy gardening"]


def test_ollama_local_only_json_and_source_redaction(monkeypatch):
    m = _module()
    for invalid in (
        "https://127.0.0.1:11434",
        "http://remote.example:11434",
        "http://127.0.0.1:11434/redirect",
        "http://user@127.0.0.1:11434",
        "http://127.0.0.1",
    ):
        with pytest.raises(ValueError, match="local HTTP"):
            m.OllamaJsonProbe(model="qwen2.5:7b", url=invalid)

    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _size):
            answer = json.dumps({
                "statement_type": "PREFERENCE", "certainty": "ASSERTED",
                "polarity": "NEGATIVE", "habituality": None,
                "conditionality": False, "hypotheticality": False,
                "temporal_references": ["PRESENT"],
            })
            return json.dumps({"message": {"content": answer}}).encode()

    def fake_urlopen(req, *, timeout):
        seen.append((req.full_url, json.loads(req.data), timeout))
        return Response()

    monkeypatch.setattr(m, "urlopen", fake_urlopen)
    backend = m.OllamaJsonProbe(model="qwen2.5:7b")
    prediction = backend.predict(
        source="I no longer enjoy gardening.",
        quote="I no longer enjoy gardening",
    )
    assert prediction.polarity == "NEGATIVE"
    assert len(seen) == 1
    assert seen[0][0] == "http://127.0.0.1:11434/api/chat"
    assert seen[0][1]["stream"] is False
    assert "I no longer enjoy gardening" in seen[0][1]["messages"][1]["content"]


def test_ollama_invalid_json_fails_closed(monkeypatch):
    m = _module()

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _):
            return json.dumps({"message": {"content": '{"polarity": "NEGATIVE"}'}}).encode()

    monkeypatch.setattr(m, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ValueError):
        m.OllamaJsonProbe(model="test").predict(source="a", quote="a")


def test_cli_write_once_and_does_not_persist_text(tmp_path):
    m = _module()
    backend = ScriptedBackend(m)
    output = tmp_path / "report.json"
    args = [
        "--backend", "spacy", "--suite", str(SUITE),
        "--case-ids", "SD001", "SD007", "--report", str(output),
    ]
    assert m.main(args, backend=backend) == 0
    payload = output.read_text(encoding="utf-8")
    assert "I enjoy gardening" not in payload
    assert "only if the weather" not in payload
    report = json.loads(payload)
    assert report["sample_claims"] == 2
    assert "by_qualifier_kind" not in report
    with pytest.raises(SystemExit):
        m.main(args, backend=backend)
    assert output.read_text(encoding="utf-8") == payload


def test_subprocess_no_model_dependencies_for_parser_and_guardrails():
    result = subprocess.run(
        [sys.executable, "-c",
         "import builtins; original=builtins.__import__; "
         "builtins.__import__=lambda name,*a,**k: "
         "(_ for _ in ()).throw(ImportError(name)) "
         "if name.startswith(('transformers','spacy','torch')) "
         "else original(name,*a,**k); "
         "import sys; sys.path.insert(0, 'scripts'); "
         "from evaluate_semantic_models import ProbeLabels; "
         "assert ProbeLabels().temporal_references == ()"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
