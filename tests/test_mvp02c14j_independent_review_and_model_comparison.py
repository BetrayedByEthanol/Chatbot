"""MVP-02C.14J contracts: no fabricated independent review or automatic promotion."""
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
review = importlib.import_module("review_independent_semantic_labels")
quality = importlib.import_module("compare_independent_semantic_models")
probes = importlib.import_module("evaluate_semantic_models")

PACKET = ROOT / "scenarios" / "mvp02c14j_blind_claim_packet.json"
GOOD = {
    "statement_type": "PREFERENCE", "certainty": "ASSERTED",
    "polarity": "POSITIVE", "habituality": None,
    "conditionality": None, "hypotheticality": None,
    "temporal_references": ["PRESENT"],
}


def _prepare(tmp_path, *, disagreement=False):
    packet, digest, keys = review.load_packet(PACKET)
    assert packet["annotation_status"] == "UNLABELED_REVIEW_PACKET"
    assert packet["independent_labels_available"] is False
    assert len(keys) == 33
    output_paths = []
    for role, actor in (("reviewer", "A"), ("reviewer", "B"),
                        ("adjudicator", "C")):
        form = review.make_form(
            packet_path=PACKET, actor_id=actor, role=role)
        assert all(item["labels"] is None for item in form["items"])
        form["attestations"] = {key: True for key in form["attestations"]}
        for item in form["items"]:
            item["labels"] = dict(GOOD)
        if disagreement and actor == "B":
            form["items"][0]["labels"] = {**GOOD, "statement_type": "FACT"}
        if disagreement and role == "adjudicator":
            form["items"][0]["rationale"] = "Reviewed both independent annotations."
        output = tmp_path / f"{actor}.json"
        output.write_text(json.dumps(form), encoding="utf-8")
        output_paths.append(output)
    return output_paths


def _gold(tmp_path, *, disagreement=False):
    paths = _prepare(tmp_path, disagreement=disagreement)
    gold = review.adjudicate(
        packet_path=PACKET, reviewer_a=paths[0],
        reviewer_b=paths[1], adjudicator_form=paths[2])
    output = tmp_path / "reviewed.json"
    output.write_text(json.dumps(gold), encoding="utf-8")
    return output, paths


def test_unlabeled_packet_contains_no_model_gold_and_has_grounded_spans():
    data, digest, keys = review.load_packet(PACKET)
    assert len(data["cases"]) == 26
    assert len(keys) == 33
    assert len(digest) == 64
    for case in data["cases"]:
        for span in case["claims"]:
            assert case["text"][span["start"]:span["end"]] == span["text"]
            assert "labels" not in span
    assert not any("reference" in row for row in data["cases"])


def test_human_form_requires_all_labels_and_independence_attestations(tmp_path):
    a, b, c = _prepare(tmp_path)
    result = json.loads(a.read_text())
    result["attestations"]["annotated_independently"] = False
    a.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="attestations"):
        review.adjudicate(
            packet_path=PACKET, reviewer_a=a, reviewer_b=b, adjudicator_form=c)


def test_adjudication_requires_three_distinct_reviewers(tmp_path):
    a, b, c = _prepare(tmp_path)
    result = json.loads(c.read_text())
    result["actor_id"] = "A"
    c.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="distinct actors"):
        review.adjudicate(
            packet_path=PACKET, reviewer_a=a, reviewer_b=b, adjudicator_form=c)


def test_disagreement_requires_separate_adjudication_rationale(tmp_path):
    a, b, c = _prepare(tmp_path, disagreement=True)
    form = json.loads(c.read_text())
    form["items"][0]["rationale"] = ""
    c.write_text(json.dumps(form))
    with pytest.raises(ValueError, match="rationale required"):
        review.adjudicate(
            packet_path=PACKET, reviewer_a=a, reviewer_b=b, adjudicator_form=c)


def test_reference_file_contains_only_adjudicator_labels_not_draft_model_gold(tmp_path):
    gold, _ = _gold(tmp_path, disagreement=True)
    value = json.loads(gold.read_text())
    assert value["claim_count"] == 33
    assert value["annotation_status"] == review.GOLD_STATUS
    assert value["disagreements_by_dimension"]["statement_type"] == 1
    assert value["review_provenance"]["reviewer_a_id"] == "A"
    checked, digest, packet_sha = quality.load_reviewed_reference(
        gold_path=gold, packet_path=PACKET)
    assert checked == value
    assert len(digest) == len(packet_sha) == 64


def test_packet_mutation_or_case_text_changes_are_rejected(tmp_path):
    gold, _ = _gold(tmp_path)
    value = json.loads(gold.read_text())
    value["cases"][0]["claims"][0]["text"] = "tampered"
    gold.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="span mismatch"):
        quality.load_reviewed_reference(gold_path=gold, packet_path=PACKET)


def test_rejects_unlabeled_or_provisional_suites_as_gold(tmp_path):
    fake_gold = tmp_path / "fake-gold.json"
    fake_gold.write_text(PACKET.read_text())
    with pytest.raises(ValueError, match="unreviewed"):
        quality.load_reviewed_reference(
            gold_path=fake_gold, packet_path=PACKET)


class StubProbe:
    backend_id = "review.test.stub"
    def __init__(self, *, result=GOOD, fail=False):
        self.seen = []
        self.result = result
        self.fail = fail
    def predict(self, *, source, quote):
        self.seen.append((source, quote))
        assert "labels" not in source and "labels" not in quote
        if self.fail:
            raise ValueError("PRIVATE source and response text must not be archived")
        return probes.ProbeLabels.from_mapping(self.result)


def _fingerprint(**_):
    return {"model_name": "test-local", "digest_verified": True,
            "digest": "d" * 64}


def test_actual_reviewed_scoring_uses_labels_only_for_metrics(tmp_path):
    gold, _ = _gold(tmp_path)
    backend = StubProbe()
    report = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=backend,
        backend_name="ollama", model_name="test-local", repetitions=2,
        provenance_resolver=_fingerprint)
    assert report["selection_decision"] == "NO_MODEL_SELECTED"
    assert len(backend.seen) == 66
    assert report["sample_claims"] == 33
    assert all(run["errors"] == 0 for run in report["runs"])
    assert all(run["by_dimension"]["statement_type"]["correct"] == 33
               for run in report["runs"])
    assert all(run["by_dimension"]["habituality"]["eligible"] == 0
               for run in report["runs"])
    assert "PRIVATE" not in json.dumps(report)
    assert "On Tuesdays" not in json.dumps(report)


def test_failed_calls_count_as_abstention_not_incorrect_predictions(tmp_path):
    gold, _ = _gold(tmp_path)
    report = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET,
        backend=StubProbe(fail=True), backend_name="ollama",
        model_name="test-local", repetitions=1,
        provenance_resolver=_fingerprint)
    run = report["runs"][0]
    assert run["errors"] == 33
    assert run["by_dimension"]["statement_type"]["supported"] == 0
    assert run["by_dimension"]["statement_type"]["abstained"] == 33
    assert run["by_dimension"]["statement_type"]["correct"] == 0


def test_paired_model_comparison_requires_same_gold_and_denominators(tmp_path):
    gold, _ = _gold(tmp_path)
    a = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=StubProbe(),
        backend_name="ollama", model_name="test-local", repetitions=1,
        provenance_resolver=_fingerprint)
    b = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=StubProbe(fail=True),
        backend_name="spacy", repetitions=1, provenance_resolver=_fingerprint)
    a["backend"] = "backend-a"
    b["backend"] = "backend-b"
    paths = (tmp_path / "a-report.json", tmp_path / "b-report.json")
    for path, record in zip(paths, (a, b)):
        path.write_text(json.dumps(record))
    output = quality.compare_reports(
        list(paths), gold_path=gold, packet_path=PACKET)
    assert output["decision"] == "NO_PRODUCTION_MODEL_SELECTED"
    assert output["candidates"][0]["runs"][0]["errors"] == 0
    assert output["candidates"][1]["runs"][0]["errors"] == 33
    b["runs"][0]["by_dimension"]["certainty"].update({
        "eligible": 0, "unscored_reference": 33,
        "supported": 0, "correct": 0, "incorrect": 0, "abstained": 0,
    })
    paths[1].write_text(json.dumps(b))
    with pytest.raises(ValueError, match="reference denominators"):
        quality.compare_reports(list(paths), gold_path=gold, packet_path=PACKET)


def test_unverified_reference_rejected_before_backend_is_ever_called(tmp_path):
    gold, _ = _gold(tmp_path)
    value = json.loads(gold.read_text())
    value["annotation_status"] = "AUTHOR_DRAFT_NOT_INDEPENDENT"
    gold.write_text(json.dumps(value))
    probe = StubProbe()
    with pytest.raises(ValueError, match="unreviewed"):
        quality.benchmark_reviewed(
            gold_path=gold, packet_path=PACKET,
            backend=probe, backend_name="ollama", model_name="test-local",
            provenance_resolver=_fingerprint)
    assert not probe.seen


def test_write_once_form_cli_and_bounded_repetitions(tmp_path):
    path = tmp_path / "form.json"
    args = ["form", "--packet", str(PACKET), "--role", "reviewer",
            "--actor-id", "Human-A", "--output", str(path)]
    assert review.main(args) == 0
    assert json.loads(path.read_text())["items"][0]["labels"] is None
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        review.main(args)
    assert path.read_bytes() == before
    gold, _ = _gold(tmp_path)
    with pytest.raises(ValueError, match="1..4"):
        quality.benchmark_reviewed(
            gold_path=gold, packet_path=PACKET,
            backend=StubProbe(), backend_name="spacy", repetitions=5,
            provenance_resolver=_fingerprint)


def test_rejects_uniform_reference_denominator_tampering_in_all_candidates(tmp_path):
    gold, _ = _gold(tmp_path)
    a = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=StubProbe(),
        backend_name="ollama", model_name="test-local", repetitions=1,
        provenance_resolver=_fingerprint)
    b = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=StubProbe(),
        backend_name="spacy", repetitions=1,
        provenance_resolver=_fingerprint)
    a["backend"], b["backend"] = "backend-a", "backend-b"
    # Both reports falsely claim reference certainty is unknown everywhere.
    # Mere cross-backend denominator agreement must NOT legitimize it.
    for report in (a, b):
        report["runs"][0]["by_dimension"]["certainty"].update({
            "eligible": 0, "unscored_reference": 33,
            "supported": 0, "correct": 0, "incorrect": 0, "abstained": 0,
        })
    paths = (tmp_path / "a-score.json", tmp_path / "b-score.json")
    for path, report in zip(paths, (a, b)):
        path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="disagree with reviewed fixture"):
        quality.compare_reports(
            list(paths), gold_path=gold, packet_path=PACKET)


def test_rejects_reports_without_human_review_status(tmp_path):
    gold, _ = _gold(tmp_path)
    report = quality.benchmark_reviewed(
        gold_path=gold, packet_path=PACKET, backend=StubProbe(),
        backend_name="ollama", model_name="test-local", repetitions=1,
        provenance_resolver=_fingerprint)
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report))
    report["backend"] = "other"
    report["reference_status"] = "AUTHOR_DRAFT_NOT_INDEPENDENT"
    b.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="unpaired or unreviewed"):
        quality.compare_reports([a, b], gold_path=gold, packet_path=PACKET)
