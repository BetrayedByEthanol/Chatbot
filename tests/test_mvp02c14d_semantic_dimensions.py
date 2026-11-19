"""MVP-02C.14D offline contract tests; all labels are provisional.

No model is run. Mirroring an author draft verifies evaluator wiring only;
it is NOT a model-quality benchmark or independent ground-truth validation.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from pydantic import ValidationError

from persona_agent.semantic_dimension_contracts import (
    SemanticDimensionsV2, SourceSpan, TAXONOMY_VERSION, validate_semantic_dimensions,
)
from persona_agent.semantic_dimension_evaluation import evaluate_dimension_pairs
from persona_agent.semantic_learning_candidates import (
    LearningEvidenceRef, SemanticLearningCandidate, TrainingUseContext,
    assess_training_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "scenarios" / "mvp02c14d_semantic_drafts.json"
OLD_SUITE = ROOT / "scenarios" / "mvp02c13_semantic_scope_corpus.json"
SCRIPT = ROOT / "scripts" / "evaluate_semantic_dimensions.py"
USER, INSTANCE, OTHER = UUID(int=1), UUID(int=2), UUID(int=3)
CANDIDATE_ID, REVIEW = UUID(int=4), UUID(int=5)


def _examples():
    return json.loads(SUITE.read_text(encoding="utf-8"))["examples"]


def _case(case_id):
    row = next(row for row in _examples() if row["id"] == case_id)
    annotated = SemanticDimensionsV2.model_validate_json(
        json.dumps(row["reference"])
    )
    validate_semantic_dimensions(
        annotated, source_turn_id=annotated.source_turn_id,
        source_text=row["text"],
    )
    return row["text"], annotated


def _candidate(*, case="SD015", status="UNREVIEWED", privacy="NORMAL",
               user_id=USER, instance_id=INSTANCE):
    _text, interpretation = _case(case)
    return SemanticLearningCandidate(
        candidate_id=CANDIDATE_ID, user_id=user_id, instance_id=instance_id,
        interpretation=interpretation, trigger="USER_CLARIFICATION",
        evidence_refs=(LearningEvidenceRef(
            source_turn_id=interpretation.source_turn_id,
            kind="USER_CLARIFICATION",
        ),),
        review_status=status, review_ref=REVIEW if status == "INDEPENDENTLY_REVIEWED" else None,
        privacy_class=privacy,
    )


def _policy(candidate, **overrides):
    data = dict(
        user_id=candidate.user_id, instance_id=candidate.instance_id,
        explicit_training_consent=True, retention_permits_training=True,
        authorized_turn_ids=frozenset({candidate.interpretation.source_turn_id}),
        independently_verified_review_refs=frozenset({REVIEW}),
    )
    data.update(overrides)
    return TrainingUseContext(**data)


def test_new_dataset_is_versioned_provisional_and_old_benchmark_is_untouched():
    data = json.loads(SUITE.read_text(encoding="utf-8"))
    assert data["version"] == "mvp02c14d.semantic_drafts.v1"
    assert data["annotation_status"] == "AUTHOR_DRAFT_NOT_INDEPENDENT"
    assert len(data["examples"]) == 15
    assert len({row["id"] for row in data["examples"]}) == 15
    assert all(_case(row["id"]) for row in data["examples"])
    old = json.loads(OLD_SUITE.read_text(encoding="utf-8"))
    assert old["version"] == "mvp02c13.semantic_scope_corpus.v1"
    assert len(old["examples"]) == 80


def test_multiclaim_temporal_references_and_changes_remain_separate():
    _text, changing = _case("SD002")
    assert len(changing.claims) == 2
    assert changing.claims[0].temporal_references == ("PAST",)
    assert changing.claims[1].temporal_references == ("PRESENT",)
    assert changing.relations[0].kind == "STATE_CHANGE"
    assert not changing.message_signals
    _text, intention = _case("SD009")
    assert set(intention.claims[0].temporal_references) == {"PRESENT", "FUTURE"}
    assert intention.claims[0].statement_type == "INTENTION"


def test_negative_preference_not_mistaken_for_conversational_correction():
    _text, simple = _case("SD005")
    _text, change = _case("SD003")
    _text, correction = _case("SD004")
    _text, combined = _case("SD015")
    assert not simple.message_signals and not simple.relations
    assert change.relations[0].kind == "STATE_CHANGE"
    assert not change.message_signals
    assert correction.message_signals[0].kind == "CORRECTION_CUE"
    assert not correction.relations
    assert combined.message_signals[0].kind == "CORRECTION_CUE"
    assert combined.relations[0].kind == "STATE_CHANGE"
    assert all("memory_id" not in case.model_dump()
               for case in (simple, change, correction, combined))


def test_asserted_conditions_and_hypotheticals_are_not_current_unqualified_facts():
    text, conditional = _case("SD007")
    claim = conditional.claims[0]
    assert claim.certainty == "ASSERTED"
    assert claim.conditionality is True
    assert claim.qualifiers[0].evidence.text not in claim.evidence.text
    assert claim.qualifiers[0].evidence.text in text
    _text, imaginary = _case("SD014")
    assert imaginary.claims[0].conditionality is True
    assert imaginary.claims[0].hypotheticality is True
    _text, uncertain = _case("SD006")
    assert uncertain.claims[0].certainty == "TENTATIVE"


def test_source_offsets_identity_and_relation_bounds_fail_closed():
    text, example = _case("SD002")
    original = example.claims[0].evidence
    wrong = example.claims[0].model_copy(update={
        "evidence": SourceSpan(
            start=original.start, end=original.end,
            text="I like gardening instead",
        )
    })
    forged = example.model_copy(update={"claims": (wrong, example.claims[1])})
    with pytest.raises(ValueError, match="unmatched source span"):
        validate_semantic_dimensions(
            forged, source_turn_id=example.source_turn_id, source_text=text
        )
    with pytest.raises(ValueError, match="source turn mismatch"):
        validate_semantic_dimensions(
            example, source_turn_id=UUID(int=99), source_text=text
        )
    with pytest.raises(ValidationError):
        SourceSpan(start=1, end=1, text="x")
    payload = example.model_dump(mode="json")
    payload["relations"][0]["to_claim_index"] = 9
    with pytest.raises(ValidationError, match="relation target"):
        SemanticDimensionsV2.model_validate_json(json.dumps(payload))
    payload = example.model_dump(mode="json")
    payload["claims"][0]["temporal_references"] = ["PAST", "PAST"]
    with pytest.raises(ValidationError, match="unique"):
        SemanticDimensionsV2.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("case_id,span_path", [
    ("SD001", ("claims", 0, "evidence")),
    ("SD010", ("claims", 0, "subject_evidence")),
    ("SD007", ("claims", 0, "qualifiers", 0, "evidence")),
    ("SD004", ("message_signals", 0, "evidence")),
    ("SD003", ("relations", 0, "evidence")),
])
def test_out_of_bounds_source_spans_are_rejected_for_all_evidence_kinds(
    case_id, span_path,
):
    """Even a slice-equivalent quote must not extend past the source end."""
    source_text, reference = _case(case_id)
    payload = reference.model_dump(mode="json")
    span = payload
    for part in span_path:
        span = span[part]
    span["text"] = source_text[span["start"]:]
    span["end"] = len(source_text) + 999
    # The schema alone cannot check against the original text length.
    corrupted = SemanticDimensionsV2.model_validate_json(json.dumps(payload))
    assert source_text[span["start"]:span["end"]] == span["text"]
    with pytest.raises(ValueError, match="unmatched source span"):
        validate_semantic_dimensions(
            corrupted, source_turn_id=reference.source_turn_id,
            source_text=source_text,
        )


def test_offline_evaluator_rejects_oversized_source_offsets_before_scoring(tmp_path):
    """Reject malformed model input instead of reporting missed/extra claims."""
    row = _examples()[0]
    bad = deepcopy(row["reference"])
    bad["claims"][0]["evidence"].update(
        end=999, text=row["text"],
    )
    predictions = {
        "version": "mvp02c14d.predictions.v1",
        "items": [{"id": row["id"], "interpretation": bad}],
    }
    source = tmp_path / "bad-predictions.json"
    source.write_text(json.dumps(predictions), encoding="utf-8")
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--suite", str(SUITE),
         "--predictions", str(source), "--report", str(report)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert not report.exists()
    assert row["text"] not in result.stderr


def test_unknown_is_not_false_and_extra_source_fields_are_rejected():
    text, example = _case("SD001")
    assert example.claims[0].habituality is None
    assert example.claims[0].conditionality is False
    draft = example.model_dump(mode="json")
    draft["claims"][0]["mystery"] = "new-authority"
    with pytest.raises(ValidationError):
        SemanticDimensionsV2.model_validate(draft)
    assert validate_semantic_dimensions(
        example, source_turn_id=example.source_turn_id, source_text=text
    ) is example


def test_learning_candidate_requires_review_pointer_and_does_not_inherit_model_truth():
    invalid = _candidate(status="INDEPENDENTLY_REVIEWED").model_dump(mode="json")
    invalid["review_ref"] = None
    with pytest.raises(ValidationError, match="review reference"):
        SemanticLearningCandidate.model_validate_json(json.dumps(invalid))
    pending = _candidate()
    assert assess_training_candidate(
        pending, _policy(pending)
    ).reason == "NO_INDEPENDENT_LABEL_REVIEW"
    reviewed = _candidate(status="INDEPENDENTLY_REVIEWED")
    assert assess_training_candidate(
        reviewed, _policy(reviewed)
    ).disposition == "ELIGIBLE_FOR_OFFLINE_EXPORT"
    assert assessed_scope_only(reviewed)


def assessed_scope_only(candidate):
    return not any(
        key in candidate.model_dump() for key in (
            "training_job_id", "model_weights", "trained", "authorized_command"
        )
    )


@pytest.mark.parametrize("context_change,expected", [
    ({"explicit_training_consent": False}, "NO_EXPLICIT_TRAINING_CONSENT"),
    ({"retention_permits_training": False}, "RETENTION_DISALLOWS_TRAINING"),
    ({"authorized_turn_ids": frozenset()}, "SOURCE_NOT_AUTHORIZED"),
    ({"independently_verified_review_refs": frozenset()}, "REVIEW_NOT_VERIFIED"),
    ({"instance_id": OTHER}, "CROSS_INSTANCE"),
    ({"user_id": OTHER}, "CROSS_INSTANCE"),
])
def test_learning_candidates_require_authorization_and_trusted_review(context_change, expected):
    candidate = _candidate(status="INDEPENDENTLY_REVIEWED")
    assert assess_training_candidate(
        candidate, _policy(candidate, **context_change)
    ).reason == expected


def test_learning_candidate_blocks_private_and_additional_unapproved_turns():
    candidate = _candidate(status="INDEPENDENTLY_REVIEWED", privacy="SENSITIVE")
    assert assess_training_candidate(candidate, _policy(candidate)).reason == (
        "PRIVACY_REVIEW_REQUIRED"
    )
    candidate = _candidate(status="INDEPENDENTLY_REVIEWED")
    extra = LearningEvidenceRef(
        source_turn_id=UUID(int=999), kind="MODEL_DISAGREEMENT"
    )
    with_extra = candidate.model_copy(update={
        "evidence_refs": candidate.evidence_refs + (extra,)
    })
    assert assess_training_candidate(
        with_extra, _policy(with_extra)
    ).reason == "SOURCE_NOT_AUTHORIZED"


def test_evaluator_counts_missing_claims_abstentions_unknown_and_false_cues():
    _, ref = _case("SD002")
    _, ref_other = _case("SD004")
    # Exact-span match, one missing claim. A model predicts no correction cue.
    observed = ref.model_copy(update={"claims": ref.claims[:1], "relations": ()})
    observed_other = ref_other.model_copy(update={"message_signals": ()})
    report = evaluate_dimension_pairs([
        ("SD002", ref, observed), ("SD004", ref_other, observed_other),
    ])
    assert report["claim_alignment"] == {
        "matched_claims": 2, "missed_claims": 1, "extra_claims": 0
    }
    assert report["by_message_signal"]["CORRECTION_CUE"]["fn"] == 1
    assert report["by_relation"]["STATE_CHANGE"]["fn"] == 1
    assert report["by_dimension"]["certainty"]["eligible"] == 2
    assert report["by_temporal_label"]["PAST"]["tp"] == 1


def test_sd007_dropped_condition_changes_qualifier_score_not_primary_claim_score():
    text, reference = _case("SD007")
    missing = reference.claims[0].model_copy(update={"qualifiers": ()})
    predicted = reference.model_copy(update={"claims": (missing,)})
    validate_semantic_dimensions(
        predicted, source_turn_id=reference.source_turn_id, source_text=text,
    )
    perfect = evaluate_dimension_pairs([("SD007", reference, reference)])
    lost = evaluate_dimension_pairs([("SD007", reference, predicted)])
    assert lost["claim_alignment"] == perfect["claim_alignment"] == {
        "matched_claims": 1, "missed_claims": 0, "extra_claims": 0,
    }
    assert lost["by_dimension"] == perfect["by_dimension"]
    assert perfect["qualifier_alignment"] == {
        "matched": 1, "missed": 0, "extra": 0,
    }
    assert lost["qualifier_alignment"] == {
        "matched": 0, "missed": 1, "extra": 0,
    }
    assert lost["by_qualifier_kind"]["CONDITION"] == {
        "matched": 0, "missed": 1, "extra": 0,
    }
    assert lost["cases"][0]["missed_qualifiers"] == 1
    assert lost["cases"][0]["matched_qualifiers"] == 0


def test_qualifier_kind_and_span_are_part_of_identity():
    text, reference = _case("SD007")
    original = reference.claims[0].qualifiers[0]
    moved_span = SourceSpan(
        start=original.evidence.start - 1,
        end=original.evidence.end,
        text=text[original.evidence.start - 1:original.evidence.end],
    )
    for altered in (
        original.model_copy(update={"kind": "TIME"}),
        original.model_copy(update={"evidence": moved_span}),
    ):
        edited_claim = reference.claims[0].model_copy(
            update={"qualifiers": (altered,)}
        )
        predicted = reference.model_copy(update={"claims": (edited_claim,)})
        validate_semantic_dimensions(
            predicted, source_turn_id=reference.source_turn_id,
            source_text=text,
        )
        report = evaluate_dimension_pairs([("SD007", reference, predicted)])
        assert report["qualifier_alignment"] == {
            "matched": 0, "missed": 1, "extra": 1,
        }
        assert report["by_qualifier_kind"]["CONDITION"]["missed"] == 1
        assert report["cases"][0]["extra_qualifiers"] == 1


def test_qualifier_counts_handle_duplicates_extras_and_multiple_kinds():
    _, with_condition = _case("SD007")
    _, with_time = _case("SD009")
    _, with_attribution = _case("SD010")
    condition = with_condition.claims[0].qualifiers[0]
    repeated = with_condition.claims[0].model_copy(
        update={"qualifiers": (condition, condition)}
    )
    repeated_pred = with_condition.model_copy(update={"claims": (repeated,)})
    duplicate_report = evaluate_dimension_pairs([
        ("SD007", with_condition, repeated_pred),
    ])
    assert duplicate_report["qualifier_alignment"] == {
        "matched": 1, "missed": 0, "extra": 1,
    }

    without_condition = with_condition.model_copy(update={
        "claims": (with_condition.claims[0].model_copy(
            update={"qualifiers": ()}
        ),)
    })
    report = evaluate_dimension_pairs([
        ("SD007", with_condition, without_condition),
        ("SD009", with_time, with_time),
        ("SD010", with_attribution, with_attribution),
    ])
    assert report["qualifier_alignment"] == {
        "matched": 2, "missed": 1, "extra": 0,
    }
    assert report["by_qualifier_kind"]["CONDITION"]["missed"] == 1
    assert report["by_qualifier_kind"]["TIME"]["matched"] == 1
    assert report["by_qualifier_kind"]["ATTRIBUTION"]["matched"] == 1
    assert [case["missed_qualifiers"] for case in report["cases"]] == [1, 0, 0]


def test_unexpected_qualifier_on_aligned_unqualified_claim_is_extra():
    text, reference = _case("SD001")
    fabricated = reference.claims[0].model_copy(update={
        "qualifiers": (
            # Lexically grounded does not mean semantically justified.
            # The evaluator must still flag this unexpected qualifier.
            {"kind": "TIME", "evidence": {
                "start": 0, "end": 1, "text": text[:1],
            }},
        )
    })
    # Validate the nested schema as well: model_copy(update=...) skips checks.
    payload = reference.model_dump(mode="json")
    payload["claims"][0]["qualifiers"] = list(fabricated.qualifiers)
    predicted = SemanticDimensionsV2.model_validate_json(json.dumps(payload))
    validate_semantic_dimensions(
        predicted, source_turn_id=reference.source_turn_id, source_text=text,
    )
    report = evaluate_dimension_pairs([("SD001", reference, predicted)])
    assert report["qualifier_alignment"] == {
        "matched": 0, "missed": 0, "extra": 1,
    }
    assert report["by_qualifier_kind"]["TIME"]["extra"] == 1


def test_cli_qualifier_loss_changes_text_free_report(tmp_path):
    row = next(row for row in _examples() if row["id"] == "SD007")
    prediction = deepcopy(row["reference"])
    source = tmp_path / "predictions.json"
    reports = []
    for loss in (False, True):
        case = deepcopy(prediction)
        if loss:
            case["claims"][0]["qualifiers"] = []
        source.write_text(json.dumps({
            "version": "mvp02c14d.predictions.v1",
            "items": [{"id": "SD007", "interpretation": case}],
        }), encoding="utf-8")
        output = tmp_path / ("dropped.json" if loss else "perfect.json")
        run = subprocess.run(
            [sys.executable, str(SCRIPT), "--suite", str(SUITE),
             "--predictions", str(source), "--report", str(output)],
            capture_output=True, text=True, timeout=15,
        )
        assert run.returncode == 0, run.stderr
        reports.append(json.loads(output.read_text(encoding="utf-8")))
        assert row["text"] not in output.read_text(encoding="utf-8")
    assert reports[0]["metrics"]["qualifier_alignment"]["matched"] == 1
    assert reports[1]["metrics"]["qualifier_alignment"]["missed"] == 1
    assert reports[0]["metrics"]["by_dimension"] == reports[1]["metrics"]["by_dimension"]


def test_sd002_reversed_state_change_is_missed_and_extra_not_a_match():
    source, reference = _case("SD002")
    original_relation = reference.relations[0]
    reversed_relation = original_relation.model_copy(update={
        "from_claim_index": original_relation.to_claim_index,
        "to_claim_index": original_relation.from_claim_index,
    })
    predicted = reference.model_copy(update={"relations": (reversed_relation,)})
    validate_semantic_dimensions(
        predicted, source_turn_id=reference.source_turn_id, source_text=source,
    )
    perfect = evaluate_dimension_pairs([("SD002", reference, reference)])
    reversed_report = evaluate_dimension_pairs([("SD002", reference, predicted)])
    assert perfect["relation_alignment"] == {
        "matched": 1, "missed": 0, "extra": 0,
    }
    assert reversed_report["relation_alignment"] == {
        "matched": 0, "missed": 1, "extra": 1,
    }
    assert reversed_report["by_relation"]["STATE_CHANGE"] == {
        "tp": 0, "fp": 1, "fn": 1, "tn": 0,
    }
    assert reversed_report["cases"][0]["missed_relations"] == 1
    assert reversed_report["cases"][0]["extra_relations"] == 1
    assert reversed_report["claim_alignment"] == perfect["claim_alignment"]
    assert reversed_report["by_dimension"] == perfect["by_dimension"]


def test_relation_scoring_is_stable_after_claim_reordering_and_endpoint_remapping():
    source, reference = _case("SD002")
    reordered = reference.model_copy(update={
        "claims": tuple(reversed(reference.claims)),
        "relations": (reference.relations[0].model_copy(update={
            "from_claim_index": 1, "to_claim_index": 0,
        }),),
    })
    validate_semantic_dimensions(
        reordered, source_turn_id=reference.source_turn_id, source_text=source,
    )
    report = evaluate_dimension_pairs([("SD002", reference, reordered)])
    assert report["claim_alignment"] == {
        "matched_claims": 2, "missed_claims": 0, "extra_claims": 0,
    }
    assert report["relation_alignment"] == {
        "matched": 1, "missed": 0, "extra": 0,
    }
    assert report["by_relation"]["STATE_CHANGE"]["tp"] == 1
    assert report["by_relation"]["STATE_CHANGE"]["fp"] == 0
    assert report["by_relation"]["STATE_CHANGE"]["fn"] == 0


def test_wrong_endpoint_missing_source_and_duplicate_relations_score_separately():
    source, reference = _case("SD002")
    original = reference.relations[0]
    variants = (
        (original.model_copy(update={"from_claim_index": None}), (0, 1, 1)),
        (original.model_copy(update={
            "from_claim_index": 1, "to_claim_index": 0,
        }), (0, 1, 1)),
    )
    for wrong, expected in variants:
        predicted = reference.model_copy(update={"relations": (wrong,)})
        validate_semantic_dimensions(
            predicted, source_turn_id=reference.source_turn_id, source_text=source,
        )
        scored = evaluate_dimension_pairs([("SD002", reference, predicted)])
        assert tuple(scored["relation_alignment"][key]
                     for key in ("matched", "missed", "extra")) == expected

    repeated = reference.model_copy(update={
        "relations": (original, original),
    })
    scored = evaluate_dimension_pairs([("SD002", reference, repeated)])
    assert scored["relation_alignment"] == {
        "matched": 1, "missed": 0, "extra": 1,
    }
    assert scored["by_relation"]["STATE_CHANGE"]["tp"] == 1
    assert scored["by_relation"]["STATE_CHANGE"]["fp"] == 1

    unary_source, unary = _case("SD003")
    assert unary.relations[0].from_claim_index is None
    assert evaluate_dimension_pairs([
        ("SD003", unary, unary),
    ])["relation_alignment"]["matched"] == 1


def test_relation_evidence_span_and_kind_change_count_as_missed_and_extra():
    source, reference = _case("SD002")
    original = reference.relations[0]
    altered_span = SourceSpan(
        start=original.evidence.start,
        end=original.evidence.end - 1,
        text=source[original.evidence.start:original.evidence.end - 1],
    )
    for changed in (
        original.model_copy(update={"kind": "CONTRAST"}),
        original.model_copy(update={"evidence": altered_span}),
    ):
        prediction = reference.model_copy(update={"relations": (changed,)})
        validate_semantic_dimensions(
            prediction, source_turn_id=reference.source_turn_id,
            source_text=source,
        )
        report = evaluate_dimension_pairs([("SD002", reference, prediction)])
        assert report["relation_alignment"] == {
            "matched": 0, "missed": 1, "extra": 1,
        }


def test_cli_reversed_relation_changes_text_free_report(tmp_path):
    row = next(row for row in _examples() if row["id"] == "SD002")
    source = tmp_path / "relation-predictions.json"
    reports = []
    for reverse in (False, True):
        candidate = deepcopy(row["reference"])
        if reverse:
            edge = candidate["relations"][0]
            edge["from_claim_index"], edge["to_claim_index"] = (
                edge["to_claim_index"], edge["from_claim_index"]
            )
        source.write_text(json.dumps({
            "version": "mvp02c14d.predictions.v1",
            "items": [{"id": "SD002", "interpretation": candidate}],
        }), encoding="utf-8")
        report = tmp_path / ("reversed.json" if reverse else "correct.json")
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "--suite", str(SUITE),
             "--predictions", str(source), "--report", str(report)],
            capture_output=True, text=True, timeout=15,
        )
        assert process.returncode == 0, process.stderr
        payload = report.read_text(encoding="utf-8")
        assert row["text"] not in payload
        reports.append(json.loads(payload))
    # The CLI evaluates the entire suite, even though only SD002 has an
    # injected prediction. Other cases legitimately contribute missed
    # relations to both reports. Verify SD002 directly and compare deltas.
    correct, reversed_metrics = (report["metrics"] for report in reports)
    by_case = [
        next(case for case in metrics["cases"] if case["id"] == "SD002")
        for metrics in (correct, reversed_metrics)
    ]
    assert (
        by_case[0]["matched_relations"],
        by_case[0]["missed_relations"],
        by_case[0]["extra_relations"],
    ) == (1, 0, 0)
    assert (
        by_case[1]["matched_relations"],
        by_case[1]["missed_relations"],
        by_case[1]["extra_relations"],
    ) == (0, 1, 1)
    for key, change in (("matched", -1), ("missed", 1), ("extra", 1)):
        assert (
            reversed_metrics["relation_alignment"][key]
            - correct["relation_alignment"][key]
        ) == change
    for before_case, after_case in zip(correct["cases"], reversed_metrics["cases"]):
        assert before_case["id"] == after_case["id"]
        if before_case["id"] != "SD002":
            assert before_case == after_case


def test_null_dimensions_not_rewarded_and_non_null_guesses_are_unverified():
    _, reference = _case("SD001")
    assert reference.claims[0].habituality is None
    altered_claim = reference.claims[0].model_copy(
        update={"habituality": True, "certainty": "UNKNOWN",
                "temporal_references": ()}
    )
    output = reference.model_copy(update={"claims": (altered_claim,)})
    report = evaluate_dimension_pairs([("one", reference, output)])
    assert report["by_dimension"]["habituality"]["eligible"] == 0
    assert report["by_dimension"]["habituality"]["unverified_prediction"] == 1
    assert report["by_dimension"]["certainty"]["abstained"] == 1
    assert report["by_dimension"]["certainty"]["correct"] == 0
    assert report["by_dimension"]["temporal_references"]["abstained"] == 1


def test_temporal_abstention_is_not_counted_as_negative_per_label():
    _, reference = _case("SD001")
    abstaining = reference.claims[0].model_copy(
        update={"temporal_references": ()}
    )
    prediction = reference.model_copy(update={"claims": (abstaining,)})
    report = evaluate_dimension_pairs([("SD001", reference, prediction)])
    assert report["by_dimension"]["temporal_references"]["abstained"] == 1
    for label in ("PAST", "PRESENT", "FUTURE"):
        assert report["by_temporal_label"][label] == {
            "tp": 0, "fp": 0, "fn": 0, "tn": 0,
            "abstained": 1, "unscored_reference": 0,
        }


def test_unknown_reference_temporal_does_not_imply_negative_truth():
    _, reference = _case("SD001")
    unknown = reference.claims[0].model_copy(
        update={"temporal_references": ()}
    )
    ref = reference.model_copy(update={"claims": (unknown,)})
    report = evaluate_dimension_pairs([("SD001", ref, reference)])
    for label in ("PAST", "PRESENT", "FUTURE"):
        assert report["by_temporal_label"][label] == {
            "tp": 0, "fp": 0, "fn": 0, "tn": 0,
            "abstained": 0, "unscored_reference": 1,
        }


def test_offline_cli_injected_predictions_do_not_leak_text_and_write_once(tmp_path):
    rows = _examples()
    predictions = {
        "version": "mvp02c14d.predictions.v1",
        "items": [{"id": "SD001", "interpretation": rows[0]["reference"]}],
    }
    source = tmp_path / "predictions.json"
    source.write_text(json.dumps(predictions), encoding="utf-8")
    report = tmp_path / "report.json"
    cmd = [sys.executable, str(SCRIPT), "--suite", str(SUITE),
           "--predictions", str(source), "--report", str(report)]
    first = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    raw = report.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["annotation_status"] == "AUTHOR_DRAFT_NOT_INDEPENDENT"
    assert data["prediction_count"] == 1
    assert data["missing_predictions"] == 14
    assert data["metrics"]["total"] == 15
    for row in rows:
        assert row["text"] not in raw
    second = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert second.returncode != 0
    assert report.read_text(encoding="utf-8") == raw
    bad = deepcopy(predictions)
    bad["items"][0]["id"] = "NONEXISTENT"
    source.write_text(json.dumps(bad), encoding="utf-8")
    third = subprocess.run(
        cmd[:-1] + [str(tmp_path / "other.json")],
        capture_output=True, text=True, timeout=15,
    )
    assert third.returncode != 0
    assert not (tmp_path / "other.json").exists()
