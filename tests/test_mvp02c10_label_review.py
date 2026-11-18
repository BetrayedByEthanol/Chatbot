"""MVP-02C.10: source-linked draft label review; no mutation of gold v1."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scenarios" / "mvp02c10_memory_conflict_diagnostic.json"
REVIEW = ROOT / "scenarios" / "mvp02c10_label_review_proposal.json"


def _read():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    return source, review


def test_review_is_explicitly_non_blinded_and_source_hash_matches():
    source, review = _read()
    assert source["annotation_status"] == "DRAFT_UNREVIEWED"
    assert review["source_annotation_status"] == "DRAFT_UNREVIEWED"
    assert review["review_status"] == (
        "ASSISTANT_SECOND_PASS_SCORE_EXPOSED_NOT_INDEPENDENT_ADJUDICATION"
    )
    assert review["source_suite_sha256"] == sha256(SOURCE.read_bytes()).hexdigest()
    assert review["source_suite"] == "scenarios/mvp02c10_memory_conflict_diagnostic.json"


def test_every_original_label_is_accounted_for_without_silent_mutation():
    source, review = _read()
    by_id = {c["id"]: c for c in source["examples"]}
    annotations = review["annotations"]
    assert len(by_id) == len(annotations) == 94
    assert len({row["id"] for row in annotations}) == 94
    assert {row["id"] for row in annotations} == set(by_id)
    assert Counter(row["status"] for row in annotations) == {
        "ACCEPT": 86, "REWORD": 5, "POLICY_REVIEW": 3,
    }
    assert review["counts"] == dict(Counter(row["status"] for row in annotations))
    for row in annotations:
        original = by_id[row["id"]]
        assert row["group"] == original["group"]
        assert row["original_expected_nli"] == original["expected_nli"]
        assert row["original_review_applicable"] is original["review_applicable"]
        assert row["nli_label_proposal"] == original["expected_nli"]
        assert row["review_applicability_proposal"] is original["review_applicable"]
        assert isinstance(row["rationale"], str) and row["rationale"].strip()


def test_rewrites_are_distinct_proposals_and_tentative_cases_remain_unresolved():
    source, review = _read()
    originals = {c["id"]: c for c in source["examples"]}
    changed = {row["id"]: row for row in review["annotations"]
               if row["status"] == "REWORD"}
    assert set(changed) == {"MC035", "MC039", "MC041", "MC048", "MC089"}
    for case_id, row in changed.items():
        rewrite = row["suggested_rewrite"]
        assert len(rewrite["stored_text"]) >= 3
        assert len(rewrite["candidate_text"]) >= 3
        assert (rewrite["stored_text"], rewrite["candidate_text"]) != (
            originals[case_id]["stored_text"],
            originals[case_id]["candidate_text"],
        )
    ambiguous = {row["id"] for row in review["annotations"]
                 if row["status"] == "POLICY_REVIEW"}
    assert ambiguous == {"MC071", "MC072", "MC074"}
    for row in review["annotations"]:
        if row["status"] != "REWORD":
            assert "suggested_rewrite" not in row
