"""MVP-02C.14D paired, read-only evaluation of source-grounded dimensions.

No semantic model is invoked. Exact-span matching deliberately penalizes
dropped qualifiers or altered extraction boundaries. All reference labels
remain author-draft diagnostics, not independently adjudicated ground truth.
"""
from __future__ import annotations

from collections import Counter
from typing import Sequence

from .semantic_dimension_contracts import SemanticDimensionsV2, TemporalReference, QualifierKind

DIMENSIONS = (
    "statement_type", "certainty", "habituality", "conditionality",
    "hypotheticality", "polarity", "temporal_references",
)
TEMPORAL_LABELS: tuple[TemporalReference, ...] = ("PAST", "PRESENT", "FUTURE")
SIGNALS = ("CORRECTION_CUE", "PRIOR_REFERENCE", "QUESTION", "REQUEST")
RELATIONS = ("STATE_CHANGE", "CONTRAST")
QUALIFIER_KINDS: tuple[QualifierKind, ...] = (
    "TIME", "PLACE", "CONDITION", "ATTRIBUTION", "OTHER",
)


def _unknown(dimension: str, value) -> bool:
    if dimension == "temporal_references":
        return not value
    return value is None or value == "UNKNOWN"


def _counts():
    return {
        "eligible": 0, "supported": 0, "correct": 0, "incorrect": 0,
        "abstained": 0, "unscored_reference": 0, "unverified_prediction": 0,
    }


def _confusion():
    return {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


def _binary_score(counts: dict, expected: bool, observed: bool):
    name = "tp" if expected and observed else (
        "fn" if expected else ("fp" if observed else "tn")
    )
    counts[name] += 1


def _relation_identities(interpretation: SemanticDimensionsV2) -> Counter:
    """Count directed relation identities by referenced claim *evidence*, not index.

    The source and target claim indices are local to one interpretation; using
    them directly would treat a harmless reordering as a semantic error.
    Include the relation's own evidence span and preserve duplicate proposals.
    """
    claim_spans = tuple(
        (claim.evidence.start, claim.evidence.end)
        for claim in interpretation.claims
    )
    return Counter(
        (
            relation.kind,
            relation.evidence.start, relation.evidence.end,
            (
                claim_spans[relation.from_claim_index]
                if relation.from_claim_index is not None else None
            ),
            claim_spans[relation.to_claim_index],
        )
        for relation in interpretation.relations
    )


def evaluate_dimension_pairs(
    cases: Sequence[tuple[str, SemanticDimensionsV2, SemanticDimensionsV2]],
) -> dict:
    """Compare actual structured predictions to draft labels, never gold as input.

    Claims align only by exact (start,end) evidence spans. For aligned claims,
    qualifier identity is (kind, start, end); missing, extra and misidentified
    qualifiers are counted independently of scalar dimensions. Directed
    relation identity is (kind, relation evidence span, from-claim evidence
    span or None, to-claim evidence span), so claim order is irrelevant.
    Unknown reference dimensions are unscored, rather than assumed negative.
    Qualifiers on unmatched claims are represented by claim omissions/extras,
    not double-counted as qualifier errors.
    """
    dimensions = {name: _counts() for name in DIMENSIONS}
    temporal = {
        name: {**_confusion(), "abstained": 0, "unscored_reference": 0}
        for name in TEMPORAL_LABELS
    }
    signals = {name: _confusion() for name in SIGNALS}
    relations = {name: _confusion() for name in RELATIONS}
    # A qualifier has no meaningful "true negative" in an open span space.
    qualifier_totals = Counter()
    qualifier_by_kind = {name: Counter() for name in QUALIFIER_KINDS}
    relation_totals = Counter()
    totals = Counter()
    per_case = []
    seen = set()
    for case_id, reference, prediction in cases:
        if case_id in seen:
            raise ValueError("duplicate evaluation case ID")
        seen.add(case_id)
        if reference.source_turn_id != prediction.source_turn_id:
            raise ValueError("prediction source turn differs from reference")

        def indexed(claims):
            mapping = {}
            for claim in claims:
                key = (claim.evidence.start, claim.evidence.end)
                if key in mapping:
                    raise ValueError("duplicate claim evidence span")
                mapping[key] = claim
            return mapping

        expected = indexed(reference.claims)
        observed = indexed(prediction.claims)
        common = expected.keys() & observed.keys()
        missed = len(expected.keys() - observed.keys())
        extra = len(observed.keys() - expected.keys())
        totals["matched_claims"] += len(common)
        totals["missed_claims"] += missed
        totals["extra_claims"] += extra
        case_qualifiers = Counter()
        for key in common:
            gold, proposed = expected[key], observed[key]
            # Compare full (kind, span) identity, preserving multiplicity.
            # Equal primary claim spans must not hide lost conditions,
            # temporal context or attribution qualifiers.
            ref_qualifiers = Counter(
                (q.kind, q.evidence.start, q.evidence.end)
                for q in gold.qualifiers
            )
            pred_qualifiers = Counter(
                (q.kind, q.evidence.start, q.evidence.end)
                for q in proposed.qualifiers
            )
            for outcome, differences in (
                ("matched", ref_qualifiers & pred_qualifiers),
                ("missed", ref_qualifiers - pred_qualifiers),
                ("extra", pred_qualifiers - ref_qualifiers),
            ):
                for (kind, _start, _end), count in differences.items():
                    qualifier_totals[outcome] += count
                    qualifier_by_kind[kind][outcome] += count
                    case_qualifiers[outcome] += count
            for name in DIMENSIONS:
                desired = getattr(gold, name)
                actual = getattr(proposed, name)
                counts = dimensions[name]
                if _unknown(name, desired):
                    counts["unscored_reference"] += 1
                    if not _unknown(name, actual):
                        counts["unverified_prediction"] += 1
                    continue
                counts["eligible"] += 1
                if _unknown(name, actual):
                    counts["abstained"] += 1
                else:
                    counts["supported"] += 1
                    if desired == actual or (
                        name == "temporal_references"
                        and set(desired) == set(actual)
                    ):
                        counts["correct"] += 1
                    else:
                        counts["incorrect"] += 1
            # An unresolved reference time is NOT proof of absent temporal
            # labels. Per-label confusion is scored on known-reference claims.
            if gold.temporal_references:
                if not proposed.temporal_references:
                    # Empty means unresolved; never score it as three negatives.
                    for label in TEMPORAL_LABELS:
                        temporal[label]["abstained"] += 1
                else:
                    for label in TEMPORAL_LABELS:
                        _binary_score(
                            temporal[label],
                            label in gold.temporal_references,
                            label in proposed.temporal_references,
                        )
            else:
                # Draft reference is unknown; its absence is not a negative.
                for label in TEMPORAL_LABELS:
                    temporal[label]["unscored_reference"] += 1
        ref_signals = {s.kind for s in reference.message_signals}
        pred_signals = {s.kind for s in prediction.message_signals}
        for name in SIGNALS:
            _binary_score(signals[name], name in ref_signals, name in pred_signals)
        ref_relations = _relation_identities(reference)
        pred_relations = _relation_identities(prediction)
        matched_relations = ref_relations & pred_relations
        missed_relations = ref_relations - pred_relations
        extra_relations = pred_relations - ref_relations
        for outcome, entries in (
            ("matched", matched_relations),
            ("missed", missed_relations),
            ("extra", extra_relations),
        ):
            relation_totals[outcome] += sum(entries.values())
        for name in RELATIONS:
            # Per-kind TP/FP/FN count *directed relation instances*, including
            # duplicated or incorrectly linked proposals, not mere kind presence.
            relations[name]["tp"] += sum(
                count for identity, count in matched_relations.items()
                if identity[0] == name
            )
            relations[name]["fn"] += sum(
                count for identity, count in missed_relations.items()
                if identity[0] == name
            )
            relations[name]["fp"] += sum(
                count for identity, count in extra_relations.items()
                if identity[0] == name
            )
            if not any(k[0] == name for k in ref_relations | pred_relations):
                relations[name]["tn"] += 1
        per_case.append({
            "id": case_id, "matched_claims": len(common),
            "missed_claims": missed, "extra_claims": extra,
            "matched_qualifiers": case_qualifiers["matched"],
            "missed_qualifiers": case_qualifiers["missed"],
            "extra_qualifiers": case_qualifiers["extra"],
            "matched_relations": sum(matched_relations.values()),
            "missed_relations": sum(missed_relations.values()),
            "extra_relations": sum(extra_relations.values()),
        })
    return {
        "total": len(cases),
        "claim_alignment": {name: totals[name] for name in (
            "matched_claims", "missed_claims", "extra_claims"
        )},
        "by_dimension": dimensions,
        "qualifier_alignment": {
            name: qualifier_totals[name] for name in ("matched", "missed", "extra")
        },
        "by_qualifier_kind": {
            kind: {name: counts[name] for name in ("matched", "missed", "extra")}
            for kind, counts in qualifier_by_kind.items()
        },
        "by_temporal_label": temporal,
        "by_message_signal": signals,
        "relation_alignment": {
            name: relation_totals[name] for name in ("matched", "missed", "extra")
        },
        "by_relation": relations,
        "cases": per_case,
    }
