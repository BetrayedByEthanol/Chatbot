"""MVP-02C.14K offline contracts; simulated reviewers are TEST FIXTURES ONLY."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

gate = importlib.import_module("audit_semantic_review_completion")
review = importlib.import_module("review_independent_semantic_labels")
probes = importlib.import_module("evaluate_semantic_models")

PACKET = ROOT / "scenarios" / "mvp02c14j_blind_claim_packet.json"

# These labels are NOT human judgments, gold labels or training data.
# They exist only to exercise validation and model comparison contracts.
TEST_ONLY = {
    "statement_type": "PREFERENCE",
    "certainty": "ASSERTED",
    "polarity": "POSITIVE",
    "habituality": None,
    "conditionality": None,
    "hypotheticality": None,
    "temporal_references": ["PRESENT"],
}


def _review_files(tmp_path: Path, *, disagreement=False):
    files = gate.prepare_forms(
        packet_path=PACKET, output_dir=tmp_path / "private",
        reviewer_a_id="test-only-a",
        reviewer_b_id="test-only-b",
        adjudicator_id="test-only-c",
    )
    for idx, path in enumerate(files):
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in value["items"]:
            item["labels"] = dict(TEST_ONLY)
            item["rationale"] = ""
        value["attestations"] = {key: True for key in value["attestations"]}
        if disagreement and idx == 1:
            value["items"][0]["labels"]["statement_type"] = "FACT"
        if disagreement and idx == 2:
            value["items"][0]["rationale"] = "TEST ONLY: resolved synthetic discrepancy"
        path.write_text(json.dumps(value), encoding="utf-8")
    gold = tmp_path / "private" / "adjudicated.json"
    gold.write_text(json.dumps(review.adjudicate(
        packet_path=PACKET, reviewer_a=files[0],
        reviewer_b=files[1], adjudicator_form=files[2],
    )), encoding="utf-8")
    return files, gold


class InjectedBackend:
    backend_id = "ollama.local.synthetic_test_only"

    def __init__(self, *, throw=False):
        self.calls = 0
        self.throw = throw

    def predict(self, *, source, quote):
        self.calls += 1
        assert "labels" not in source
        assert "test-only" not in source
        if self.throw:
            raise RuntimeError("SECRET_PRIVATE_MODEL_EXCEPTION")
        return probes.ProbeLabels.from_mapping(TEST_ONLY)


def _fingerprint(**_):
    return {"model_name": "synthetic-fake-test-only", "digest_verified": True,
            "digest": "d" * 64}


def _run(files, gold, backend, name="ollama"):
    return gate.run_reviewed(
        packet_path=PACKET, gold_path=gold,
        reviewer_a=files[0], reviewer_b=files[1], adjudicator_form=files[2],
        backend=backend, backend_name=name, model_name="synthetic-fake-test-only",
        repetitions=1, provenance_resolver=_fingerprint,
    )


def test_status_is_pending_and_never_implies_humans_have_annotated():
    status = gate.review_status(packet_path=PACKET)
    assert status["status"] == gate.REPORT_STATUS_PENDING
    assert status["reviewed_gold_available"] is False
    assert status["sample_cases"] == 26
    assert status["sample_claims"] == 33
    assert "labels" not in json.dumps(status)


def test_prepare_leaves_all_labels_and_attestations_blank(tmp_path):
    paths = gate.prepare_forms(
        packet_path=PACKET, output_dir=tmp_path / "review-bundle",
        reviewer_a_id="user-assigned-A", reviewer_b_id="user-assigned-B",
        adjudicator_id="user-assigned-C",
    )
    assert len(paths) == 3
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert all(row["labels"] is None for row in value["items"])
        assert all(flag is False for flag in value["attestations"].values())
        assert len(value["items"]) == 33
    with pytest.raises(ValueError, match="already exists"):
        gate.prepare_forms(
            packet_path=PACKET, output_dir=tmp_path / "review-bundle",
            reviewer_a_id="A", reviewer_b_id="B", adjudicator_id="C",
        )


def test_duplicate_actor_ids_fail_before_creating_directory(tmp_path):
    dest = tmp_path / "forms"
    with pytest.raises(ValueError, match="distinct"):
        gate.prepare_forms(packet_path=PACKET, output_dir=dest,
                           reviewer_a_id="A", reviewer_b_id="B",
                           adjudicator_id="A")
    assert not dest.exists()


def test_full_audit_text_free_and_preserves_unknown_counts(tmp_path):
    files, gold = _review_files(tmp_path, disagreement=True)
    result = gate.audit_completed_review(
        packet_path=PACKET, gold_path=gold,
        reviewer_a=files[0], reviewer_b=files[1], adjudicator_form=files[2],
    )
    assert result["status"] == gate.REPORT_STATUS_REVIEWED
    assert result["human_independence"] == gate.SEAL_STATUS
    assert result["sample_claims"] == 33
    assert result["per_dimension"]["statement_type"]["raw_disagreement_count"] == 1
    assert result["per_dimension"]["statement_type"]["raw_agreement_count"] == 32
    assert result["per_dimension"]["certainty"]["raw_agreement_fraction"] == 1.0
    assert result["per_dimension"]["habituality"]["adjudicated_unknown"] == 33
    assert set(result["below_proposed_sample_floor"]) == set(probes.LABEL_FIELDS)
    assert result["production_selection_status"] == "NOT_ELIGIBLE_FOR_PRODUCTION_SELECTION"
    saved = json.dumps(result)
    assert "On Tuesdays" not in saved
    assert "SECRET" not in saved
    assert "test-only-a" not in saved


def test_adjudicator_label_tamper_rejected_before_model_exec(tmp_path):
    files, gold = _review_files(tmp_path)
    original = json.loads(gold.read_text())
    original["cases"][0]["claims"][0]["labels"]["certainty"] = "TENTATIVE"
    gold.write_text(json.dumps(original))
    backend = InjectedBackend()
    with pytest.raises(ValueError, match="does not match original human forms"):
        _run(files, gold, backend)
    assert backend.calls == 0


def test_adjudicator_metadata_disagreement_tamper_rejected(tmp_path):
    files, gold = _review_files(tmp_path)
    original = json.loads(gold.read_text())
    original["disagreements_by_dimension"]["statement_type"] += 1
    gold.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="does not match original human forms"):
        gate.audit_completed_review(
            packet_path=PACKET, gold_path=gold,
            reviewer_a=files[0], reviewer_b=files[1], adjudicator_form=files[2],
        )


def test_uncompleted_form_cannot_trigger_model_calls(tmp_path):
    files, gold = _review_files(tmp_path)
    a = json.loads(files[0].read_text())
    a["attestations"]["annotated_independently"] = False
    files[0].write_text(json.dumps(a))
    backend = InjectedBackend()
    with pytest.raises(ValueError, match="attestations"):
        _run(files, gold, backend)
    assert backend.calls == 0


def test_full_pilot_comparison_preserves_abstention_and_no_model_promotion(tmp_path):
    files, gold = _review_files(tmp_path)
    fine = InjectedBackend()
    bad = InjectedBackend(throw=True)
    first = _run(files, gold, fine)
    second = _run(files, gold, bad)
    assert fine.calls == bad.calls == 33
    assert first["runs"][0]["errors"] == 0
    assert second["runs"][0]["errors"] == 33
    assert second["runs"][0]["by_dimension"]["statement_type"]["supported"] == 0
    assert first["review_audit_sha256"] == second["review_audit_sha256"]
    assert first["selection_decision"] == "NO_MODEL_SELECTED"
    first["backend"], second["backend"] = "test-only-A", "test-only-B"
    paths = [tmp_path / "a-report.json", tmp_path / "b-report.json"]
    for path, value in zip(paths, [first, second]):
        path.write_text(json.dumps(value))
    compared = gate.compare_reviewed(
        packet_path=PACKET, gold_path=gold,
        reviewer_a=files[0], reviewer_b=files[1], adjudicator_form=files[2],
        reports=paths,
    )
    assert compared["decision"] == "NO_PRODUCTION_MODEL_SELECTED"
    assert compared["selection_decision"] == "NO_PRODUCTION_MODEL_SELECTED"
    assert len(compared["candidates"]) == 2
    assert "On Tuesdays" not in json.dumps(compared)


def test_tampered_review_hash_invalidates_previous_model_reports(tmp_path):
    files, gold = _review_files(tmp_path)
    report = _run(files, gold, InjectedBackend())
    report["backend"] = "backend-A"
    report2 = {**report, "backend": "backend-B"}
    paths = [tmp_path / "A.json", tmp_path / "B.json"]
    for path, obj in zip(paths, (report, report2)):
        path.write_text(json.dumps(obj))
    updated = json.loads(files[0].read_text())
    updated["items"][0]["rationale"] = "changed comment after sealing"
    files[0].write_text(json.dumps(updated))
    with pytest.raises(ValueError, match="does not match original human forms"):
        gate.compare_reviewed(
            packet_path=PACKET, gold_path=gold,
            reviewer_a=files[0], reviewer_b=files[1],
            adjudicator_form=files[2], reports=paths,
        )


def test_old_ungated_reports_rejected_even_when_gold_hashes_match(tmp_path):
    files, gold = _review_files(tmp_path)
    first = _run(files, gold, InjectedBackend())
    second = {**first, "backend": "another-backend"}
    del second["review_gate_version"]
    paths = [tmp_path / "A.json", tmp_path / "B.json"]
    for path, obj in zip(paths, (first, second)):
        path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match="not gated"):
        gate.compare_reviewed(
            packet_path=PACKET, gold_path=gold,
            reviewer_a=files[0], reviewer_b=files[1],
            adjudicator_form=files[2], reports=paths,
        )


def test_cli_pending_report_is_write_once(tmp_path):
    output = tmp_path / "review-status.json"
    args = ["status", "--packet", str(PACKET), "--report", str(output)]
    assert gate.main(args) == 0
    recorded = output.read_bytes()
    assert json.loads(recorded)["status"] == gate.REPORT_STATUS_PENDING
    with pytest.raises(SystemExit):
        gate.main(args)
    assert output.read_bytes() == recorded


def test_cli_unreviewed_input_does_not_create_benchmark_report(tmp_path):
    outfile = tmp_path / "fake-score.json"
    args = [
        "run", "--packet", str(PACKET), "--gold", str(tmp_path / "missing.json"),
        "--reviewer-a", str(tmp_path / "no-a.json"),
        "--reviewer-b", str(tmp_path / "no-b.json"),
        "--adjudicator", str(tmp_path / "no-c.json"),
        "--backend", "ollama", "--model", "qwen2.5:0.5b",
        "--report", str(outfile),
    ]
    with pytest.raises(SystemExit):
        gate.main(args)
    assert not outfile.exists()
