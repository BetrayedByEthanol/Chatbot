"""MVP-02C.13: source-grounded scope diagnostic and blinded review workflow.

All test labels are synthetic/author draft, never treated as independent gold.
No production Memory, Conversation, or NLI model state is modified.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_semantic_scope.py"
CORPUS = ROOT / "scenarios" / "mvp02c13_semantic_scope_corpus.json"
PACKET = ROOT / "scenarios" / "mvp02c13_blind_review_packet.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("mvp02c13_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dataset():
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def test_provisional_corpus_has_broad_relevant_coverage_and_untouched_blind_packet():
    module = _load_script()
    cases, digest = module.load_corpus(CORPUS)
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    assert len(cases) == len(packet["examples"]) == 80
    assert len({r["id"] for r in cases}) == 80
    assert len({r["group"] for r in cases}) == 9
    assert sum(row["draft"]["narrow_current_scope"] for row in cases) == 28
    assert digest == sha256(CORPUS.read_bytes()).hexdigest()
    assert packet["annotation_status"] == "UNLABELED_REVIEW_PACKET"
    assert packet["version"] == "mvp02c13.blind_annotation_packet.v1"
    assert [(r["id"], r["text"]) for r in cases] == [
        (r["id"], r["text"]) for r in packet["examples"]]
    assert all(set(r) == {"id", "text"} for r in packet["examples"])
    assert "not independently" in _dataset()["provenance"].lower()


def test_actual_stage_a_and_existing_memory_grammar_are_scored_not_oracle_inputs():
    module = _load_script()
    report = module.evaluate_corpus(CORPUS)
    m = report["metrics"]
    assert m["total"] == 80
    assert report["annotation_status"] == "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"
    assert m["stage_a_coverage"] > 0
    assert 0 < m["scope_coverage"] < m["total"]
    assert m["narrow_confusion"]["fp"] == 0
    assert m["narrow_confusion"]["fn"] > 0  # Not a perfect extractor.
    assert m["narrow_confusion"]["tp"] == m["scope_coverage"]
    assert m["narrow_confusion"]["tp"] + m["narrow_confusion"]["fn"] == 28
    assert len(report["cases"]) == m["total"]
    assert set(m["by_group"]) == {row["group"] for row in _dataset()["examples"]}
    assert m["by_dimension"]["modality"]["supported"] > 0
    assert m["by_dimension"]["subject_key"]["abstained"] > 0
    assert "NOT independently adjudicated" in " ".join(report["limits"])
    assert all("text" not in row and "input_text" not in row and "value_text" not in row
               for row in report["cases"])


@pytest.mark.parametrize("text,narrow", [
    ("I enjoy gardening.", True),
    ("I really enjoy reading.", True),
    ("I don't enjoy gardening anymore.", True),
    ("I used to enjoy gardening.", False),
    ("I enjoyed gardening yesterday.", False),
    ("Maybe I should go walking tomorrow.", False),
    ("I've gotten into gardening lately.", False),  # Stage A only: no canonical slot
    ("I enjoy gardening only during summer.", False),
    ("I enjoy gardening on weekends.", False),
    ("I enjoy gardening. I no longer do.", False),
    ("My brother enjoys gardening.", False),
    ('My friend said, "I enjoy gardening."', False),
    ("I might enjoy gardening.", False),
    ("If I had more time, I would enjoy gardening.", False),
    ("Actually, I don't enjoy gardening anymore.", False),  # baseline doesn't parse prefix
])
def test_full_original_source_is_required_for_narrow_scope(text, narrow):
    got = _load_script().extract_scope(text, "SC999")
    assert (got["narrow_scope"] is not None) is narrow
    if narrow:
        assert got["narrow_scope"]["context_key"] == "general"
        assert got["narrow_scope"]["subject_key"] == "user"
    else:
        assert got["status"] in ("ABSTAIN", "SEMANTIC_ONLY")


def test_injected_classifier_cannot_supply_forged_quote_as_grounding():
    module = _load_script()
    class BadModel:
        def classify(self, text, *, source_turn_id):
            batch = module.RuleSemanticClassifier().classify(
                text, source_turn_id=source_turn_id)
            claim = batch.claims[0].model_copy(
                update={"evidence_quote": "I enjoy walking."})
            return batch.model_copy(update={"claims": (claim,)})
    with pytest.raises(ValueError, match="not verbatim"):
        module.extract_scope("I enjoy gardening.", "SC001", classifier=BadModel())


def test_bad_corpus_labels_are_rejected_without_gold_repair(tmp_path):
    module = _load_script()
    corpus = _dataset()
    variants = (
        (lambda d: d.update(annotation_status="INDEPENDENTLY_ADJUDICATED"), "promoted"),
        (lambda d: d["examples"].append(deepcopy(d["examples"][0])), "duplicate"),
        (lambda d: d["examples"][0]["draft"].update(context_key="UNKNOWN"), "invalid canonical"),
        (lambda d: d["examples"][0]["draft"].update(narrow_current_scope=False),
         None),  # Allowed disagreement: do not silently fix author labels.
        (lambda d: d["examples"][0]["draft"].update(modality="TRUTH"), "unsupported"),
        (lambda d: d["examples"][0]["draft"].update(narrow_current_scope="true"), "must be bool"),
    )
    for i, (change, message) in enumerate(variants):
        modified = deepcopy(corpus)
        change(modified)
        file = tmp_path / f"corpus-{i}.json"
        file.write_text(json.dumps(modified), encoding="utf-8")
        if message is None:
            # Reviewer disagreements are legitimate; only schema is enforced.
            assert module.load_corpus(file)[0][0]["draft"]["narrow_current_scope"] is False
        else:
            with pytest.raises(ValueError):
                module.load_corpus(file)


def test_blind_template_is_prediction_free_and_hash_bound():
    module = _load_script()
    template = module.make_blind_template(CORPUS)
    assert template["reviewer_id"] is None
    assert template["source_sha256"] == sha256(CORPUS.read_bytes()).hexdigest()
    assert len(template["annotations"]) == 80
    for row in template["annotations"]:
        assert row["labels"] == {k: None for k in module.FIELDS}
        assert "draft" not in row
        assert "predicted" not in row
    assert all(set(row) == {"id", "text", "labels"} for row in template["annotations"])


def _fake_review(module, reviewer, *, edit_case=None):
    """Fabricate metadata solely for harness tests, never genuine review."""
    template = module.make_blind_template(CORPUS)
    draft = {x["id"]: x["draft"] for x in _dataset()["examples"]}
    template["reviewer_id"] = reviewer
    for row in template["annotations"]:
        row["labels"] = deepcopy(draft[row["id"]])
        if row["id"] == edit_case:
            row["labels"]["modality"] = "UNKNOWN"
    return template


def test_review_comparison_records_disagreements_but_never_promotes_gold(tmp_path):
    module = _load_script()
    a = tmp_path / "review-a.json"
    b = tmp_path / "review-b.json"
    a.write_text(json.dumps(_fake_review(module, "reviewer_a")), encoding="utf-8")
    b.write_text(json.dumps(_fake_review(module, "reviewer_b", edit_case="SC025")),
                 encoding="utf-8")
    report = module.compare_reviews(CORPUS, a, b)
    assert report["total"] == 80
    assert report["full_agreement"] == 79
    assert report["disagreements"] == [{"id": "SC025", "different_fields": ["modality"]}]
    assert report["status"] == "REQUIRES_INDEPENDENT_ADJUDICATION"
    assert report["per_field_agreements"]["modality"] == 79
    assert report["per_field_agreements"]["temporal_scope"] == 80
    assert "independence" in report["warning"].lower()
    source = _dataset()
    assert source["annotation_status"] == "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"


def test_incomplete_or_stale_review_fails_closed(tmp_path):
    module = _load_script()
    a = tmp_path / "review-a.json"
    b = tmp_path / "review-b.json"
    a.write_text(json.dumps(_fake_review(module, "reviewer_a")), encoding="utf-8")
    draft = _fake_review(module, "reviewer_b")
    draft["source_sha256"] = "0" * 64
    b.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        module.compare_reviews(CORPUS, a, b)
    draft = _fake_review(module, "reviewer_b")
    draft["annotations"].pop()
    b.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(ValueError, match="annotate every case"):
        module.compare_reviews(CORPUS, a, b)
    draft = _fake_review(module, "reviewer_a")
    b.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct reviewer"):
        module.compare_reviews(CORPUS, a, b)


def test_offline_cli_writes_single_unmodified_report_and_rejects_overwrite(tmp_path):
    out = tmp_path / "scope-evaluation.json"
    cmd = [sys.executable, str(SCRIPT), "--suite", str(CORPUS), "--report", str(out)]
    result = run(cmd, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["metrics"]["total"] == 80
    previous = out.read_bytes()
    result = run(cmd, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert out.read_bytes() == previous


def test_blind_template_cli_never_includes_predictions(tmp_path):
    out = tmp_path / "blind-template.json"
    result = run(
        [sys.executable, str(SCRIPT), "--suite", str(CORPUS),
         "--blind-template", "--report", str(out)],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    packet = json.loads(out.read_text(encoding="utf-8"))
    assert len(packet["annotations"]) == 80
    assert packet["annotations"][0]["labels"]["narrow_current_scope"] is None
    assert "metrics" not in packet
