"""MVP-02C.14H: privacy-safe real-model failure diagnostic contracts."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
probe_mod = importlib.import_module("evaluate_semantic_models")
diag = importlib.import_module("diagnose_semantic_model_failures")
SUITE = ROOT / "scenarios" / "mvp02c14d_semantic_drafts.json"

GOOD_LABELS = {
    "statement_type": "PREFERENCE", "certainty": "ASSERTED",
    "polarity": "POSITIVE", "habituality": None,
    "conditionality": False, "hypotheticality": False,
    "temporal_references": ["PRESENT"],
}


class Response:
    status = 200

    def __init__(self, content: bytes):
        self.content = content

    def read(self, _):
        return self.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _response(content, *, done_reason="stop"):
    return Response(json.dumps({
        "message": {"content": content},
        "done_reason": done_reason,
    }).encode())


def _fake_probe(monkeypatch, *, mode="json", response=None, error=None):
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append((json.loads(request.data), timeout))
            if error:
                raise error
            return response

    monkeypatch.setattr(probe_mod, "build_opener", lambda _proxy: Opener())
    codes = []
    backend = probe_mod.OllamaJsonProbe(
        model="qwen2.5:0.5b", format_mode=mode,
        diagnostic_sink=codes.append,
    )
    return backend, codes, requests


def test_json_mode_remains_original_wire_format_and_does_not_leak(monkeypatch):
    backend, codes, requests = _fake_probe(
        monkeypatch, response=_response(json.dumps(GOOD_LABELS)))
    result = backend.predict(
        source="SECRET_source_never_in_report",
        quote="SECRET_quote_never_in_report",
    )
    assert result.statement_type == "PREFERENCE"
    assert codes == ["VALID_PREDICTION"]
    assert requests[0][0]["format"] == "json"
    assert "SECRET" not in json.dumps(codes)


def test_schema_mode_is_explicit_and_strict(monkeypatch):
    backend, codes, requests = _fake_probe(
        monkeypatch, mode="schema", response=_response(json.dumps(GOOD_LABELS)))
    backend.predict(source="private", quote="private")
    fmt = requests[0][0]["format"]
    assert fmt["type"] == "object"
    assert fmt["additionalProperties"] is False
    assert set(fmt["required"]) == set(probe_mod.LABEL_FIELDS)
    assert fmt["properties"]["certainty"]["enum"] == [
        "ASSERTED", "TENTATIVE", "UNKNOWN",
    ]
    assert codes == ["VALID_PREDICTION"]


@pytest.mark.parametrize("content,expected", [
    ("not-json-or-source-SECRET", "CONTENT_JSON_INVALID"),
    ('{"statement_type":"FACT"}', "LABEL_FIELD_SET_INVALID"),
    ('[]', "LABEL_NOT_OBJECT"),
    (json.dumps({**GOOD_LABELS, "polarity": "INVALID"}), "LABEL_VALUE_INVALID"),
    ("", "CONTENT_EMPTY"),
])
def test_invalid_ollama_content_classified_without_original_text(
    monkeypatch, content, expected,
):
    backend, codes, _ = _fake_probe(
        monkeypatch, response=_response(content))
    with pytest.raises(Exception):
        backend.predict(source="SECRET_private", quote="SECRET_claim")
    assert codes == [expected]
    assert "SECRET" not in json.dumps(codes)
    assert content not in json.dumps(codes)


@pytest.mark.parametrize("raw,expected", [
    (b"garbled response", "ENVELOPE_JSON_INVALID"),
    (b'{"message":{}}', "CONTENT_INVALID"),
    (b'{"bad":"envelope"}', "ENVELOPE_INVALID"),
])
def test_invalid_ollama_envelope_redacted(monkeypatch, raw, expected):
    backend, codes, _ = _fake_probe(monkeypatch, response=Response(raw))
    with pytest.raises(Exception):
        backend.predict(source="SECRET", quote="SECRET")
    assert codes == [expected]


def test_truncated_response_distinct_from_invalid_json(monkeypatch):
    backend, codes, _ = _fake_probe(
        monkeypatch, response=_response('{"incomplete":', done_reason="length"))
    with pytest.raises(ValueError):
        backend.predict(source="SECRET", quote="SECRET")
    assert codes == ["OUTPUT_TRUNCATED"]


@pytest.mark.parametrize("error,expected", [
    (HTTPError("http://127.0.0.1:11434", 422, "SECRET", None, None), "HTTP_4XX"),
    (HTTPError("http://127.0.0.1:11434", 500, "SECRET", None, None), "HTTP_5XX"),
    (URLError("SECRET_proxy_message"), "TRANSPORT_ERROR"),
    (TimeoutError("SECRET_timeout_message"), "TRANSPORT_TIMEOUT"),
])
def test_http_and_transport_error_codes_have_no_exception_message(
    monkeypatch, error, expected,
):
    backend, codes, _ = _fake_probe(monkeypatch, error=error)
    with pytest.raises(Exception):
        backend.predict(source="SECRET", quote="SECRET")
    assert codes == [expected]
    assert "SECRET" not in json.dumps(codes)


class DeterministicDiagnosticBackend:
    backend_id = "ollama.local_json.qwen2.5:0.5b"

    def __init__(self, *, mode, sink):
        self.mode, self.sink = mode, sink
        self.calls = 0

    def predict(self, *, source, quote):
        self.calls += 1
        if self.mode == "json":
            self.sink("LABEL_FIELD_SET_INVALID")
            raise ValueError("SECRET source " + source)
        self.sink("VALID_PREDICTION")
        return probe_mod.ProbeLabels.from_mapping(GOOD_LABELS)


def _factory(*, format_mode, diagnostic_sink, **_):
    return DeterministicDiagnosticBackend(
        mode=format_mode, sink=diagnostic_sink,
    )


def _identity(**_):
    return {"model_name": "qwen2.5:0.5b", "digest_verified": True,
            "digest": "a" * 64}


def test_diagnostic_comparison_same_real_contract_and_no_raw_text():
    report = diag.run_diagnostics(
        model="qwen2.5:0.5b", endpoint="http://127.0.0.1:11434",
        timeout_s=60, suite=SUITE, case_ids=("SD001", "SD002"),
        backend_factory=_factory, provenance_resolver=_identity,
    )
    assert report["decision"] == "NO_PRODUCTION_MODEL_SELECTED"
    assert report["sample_claims"] == 3
    assert report["case_ids"] == ["SD001", "SD002"]
    assert [r["mode"] for r in report["modes"]] == ["json", "schema"]
    a, b = report["modes"]
    assert a["status"] == "NO_VALID_PREDICTIONS"
    assert a["error_claims"] == 3
    assert a["failure_histogram"] == {"LABEL_FIELD_SET_INVALID": 3}
    assert a["warmup_code"] == "LABEL_FIELD_SET_INVALID"
    assert b["status"] == "NO_PREDICTION_FAILURES_OBSERVED"
    assert b["valid_prediction_claims"] == 3
    assert b["failure_histogram"] == {}
    assert b["warmup_code"] == "VALID_PREDICTION"
    assert b["format_schema_sha256"]
    assert a["format_schema_sha256"] is None
    assert "SECRET" not in json.dumps(report)
    assert "I enjoy gardening" not in json.dumps(report)


def test_nonmatching_diagnostic_event_counts_are_rejected():
    def bad_factory(**_):
        class Backend:
            backend_id = "ollama.fake"
            def predict(self, *, source, quote):
                return probe_mod.ProbeLabels.from_mapping(GOOD_LABELS)
        return Backend()
    with pytest.raises(ValueError, match="warmup diagnostic event"):
        diag.run_diagnostics(
            model="qwen2.5:0.5b", endpoint="http://127.0.0.1:11434",
            timeout_s=60, suite=SUITE, case_ids=("SD001",),
            backend_factory=bad_factory, provenance_resolver=_identity,
        )


def test_model_fingerprint_drift_fails_closed():
    seen = 0
    def changed(**_):
        nonlocal seen
        seen += 1
        return {"digest_verified": True, "digest": str(seen) * 64}
    with pytest.raises(ValueError, match="digest/provenance"):
        diag.run_diagnostics(
            model="qwen2.5:0.5b", endpoint="http://127.0.0.1:11434",
            timeout_s=60, suite=SUITE, case_ids=("SD001",),
            backend_factory=_factory, provenance_resolver=changed,
        )


def test_mode_validation_and_write_once_cli(tmp_path):
    args = dict(
        model="qwen2.5:0.5b", endpoint="http://127.0.0.1:11434",
        timeout_s=60, suite=SUITE,
        case_ids=("SD001",),
        backend_factory=_factory, provenance_resolver=_identity,
    )
    with pytest.raises(ValueError, match="distinct"):
        diag.run_diagnostics(**args, modes=("schema", "schema"))
    report = tmp_path / "result.json"
    argv = [
        "--model", "qwen2.5:0.5b", "--case-ids", "SD001",
        "--report", str(report),
    ]
    assert diag.main(argv, backend_factory=_factory, provenance_resolver=_identity) == 0
    recorded = report.read_bytes()
    assert json.loads(recorded)["modes"][0]["sample_claims"] == 1
    with pytest.raises(SystemExit):
        diag.main(argv, backend_factory=_factory, provenance_resolver=_identity)
    assert report.read_bytes() == recorded


def test_rejects_unknown_codes():
    with pytest.raises(ValueError, match="unknown diagnostic event"):
        diag._record_one("SECRET_source", [])
