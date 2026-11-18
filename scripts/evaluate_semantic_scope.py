#!/usr/bin/env python3
"""MVP-02C.13: read-only semantic and scope extraction diagnostic.

Only existing Stage A deterministic annotations and Memory's allowlisted
explicit-claim grammar are used. This is an OFFLINE evaluation on synthetic
AUTHOR-LABELED examples, NOT an independently validated extraction model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from persona_agent.memory_explicit_grammar import parse_explicit_claim
from persona_agent.semantic_classification import RuleSemanticClassifier
from persona_agent.semantic_contracts import validate_semantic_evidence

DATASET_VERSION = "mvp02c13.semantic_scope_corpus.v1"
LABEL_STATUS = "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT"
FIELDS = ("statement_type", "temporal_scope", "modality", "pragmatic_function",
          "subject_key", "predicate_key", "context_key", "normalized_value",
          "narrow_current_scope")
DIMENSIONS = FIELDS[:-1]
CLAIM_LABELS = {
    "statement_type": frozenset(("FACT", "PREFERENCE", "EXPERIENCE", "INTENTION", "OPINION", "UNKNOWN")),
    "temporal_scope": frozenset(("CURRENT", "HABITUAL", "PAST_EVENT", "PAST_HABITUAL", "FUTURE", "UNSPECIFIED")),
    "modality": frozenset(("ASSERTED", "TENTATIVE", "CONDITIONAL", "HYPOTHETICAL", "UNKNOWN")),
    "pragmatic_function": frozenset(("INFORMING", "REQUESTING", "CORRECTING", "ASKING", "UNKNOWN")),
}
NARROW_PREDICATES = frozenset(("enjoys_gardening", "enjoys_reading", "enjoys_walking"))
KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def read_json(path: Path, limit: int = 160_000):
    raw = Path(path).read_bytes()
    if len(raw) > limit:
        raise ValueError("evaluation JSON exceeds file size limit")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")
    return obj, sha256(raw).hexdigest()


def check_labels(labels: dict) -> None:
    if not isinstance(labels, dict) or set(labels) != set(FIELDS):
        raise ValueError("missing or unexpected semantic scope labels")
    for key, allowed in CLAIM_LABELS.items():
        if labels[key] not in allowed:
            raise ValueError("unsupported semantic label: " + key)
    for key in ("subject_key", "predicate_key", "context_key"):
        v = labels[key]
        if v is not None and (not isinstance(v, str) or KEY.fullmatch(v) is None):
            raise ValueError("invalid canonical key: " + key)
    if labels["normalized_value"] not in (None, "yes", "no"):
        raise ValueError("invalid normalized value")
    if type(labels["narrow_current_scope"]) is not bool:
        raise ValueError("narrow_current_scope must be bool")
    if labels["narrow_current_scope"] and not (
        labels["statement_type"] == "PREFERENCE"
        and labels["temporal_scope"] == "CURRENT"
        and labels["modality"] == "ASSERTED"
        and labels["pragmatic_function"] in ("INFORMING", "CORRECTING")
        and labels["subject_key"] == "user"
        and labels["predicate_key"] in NARROW_PREDICATES
        and labels["context_key"] == "general"
        and labels["normalized_value"] in ("yes", "no")
    ):
        raise ValueError("inconsistent narrow-current-scope gold labels")


def load_corpus(path: Path):
    obj, digest = read_json(path)
    if obj.get("version") != DATASET_VERSION or obj.get("annotation_status") != LABEL_STATUS:
        raise ValueError("unsupported or incorrectly promoted diagnostic dataset")
    rows = obj.get("examples")
    if not isinstance(rows, list) or not 20 <= len(rows) <= 250:
        raise ValueError("expected 20..250 synthetic scope examples")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "group", "text", "draft"}:
            raise ValueError("invalid scope example fields")
        key, group, value = row["id"], row["group"], row["text"]
        if (not isinstance(key, str) or not re.fullmatch(r"SC[0-9]{3}", key)
            or key in ids or not isinstance(group, str)
            or re.fullmatch(r"[a-z_]{3,40}", group) is None
            or not isinstance(value, str) or not 3 <= len(value) <= 500
            or value != value.strip() or "\n" in value):
            raise ValueError("invalid/duplicate ID, group or source text")
        ids.add(key)
        check_labels(row["draft"])
    return rows, digest


def extract_scope(text: str, case_id: str, *, classifier=None) -> dict:
    """Actual existing deterministic interpretation from the FULL source text.

    Important: this evaluator receives offline example text, NOT a trusted
    turn from PostgreSQL. No authenticated status or Memory admission is
    inferred. Scope comes only from both FULL text and verbatim evidence.
    """
    turn_id = uuid5(NAMESPACE_URL, f"mvp02c13/{case_id}")
    adapter = classifier if classifier is not None else RuleSemanticClassifier()
    batch = validate_semantic_evidence(
        adapter.classify(text, source_turn_id=turn_id),
        source_turn_id=turn_id, source_text=text,
    )
    if len(batch.claims) != 1:
        return {"status": "ABSTAIN", "reason": batch.abstention_reason or "MULTIPLE_CLAIMS",
                "classifier_id": batch.classifier_id,
                "labels": None, "narrow_scope": None}
    claim = batch.claims[0]
    labels = {key: getattr(claim, key) for key in CLAIM_LABELS}
    labels.update({"subject_key": None, "predicate_key": None,
                   "context_key": None, "normalized_value": None})
    parsed = parse_explicit_claim(text)
    if (claim.evidence_quote != text or parsed is None
            or parsed.memory_kind != "PREFERENCE"
            or parsed.subject_key != "user"
            or parsed.predicate_key not in NARROW_PREDICATES
            or claim.statement_type != "PREFERENCE"
            or claim.temporal_scope != "CURRENT"
            or claim.modality != "ASSERTED"
            or claim.pragmatic_function not in ("INFORMING", "CORRECTING")
            or claim.topic_label != parsed.predicate_key.removeprefix("enjoys_")):
        return {"status": "SEMANTIC_ONLY", "reason": "NO_GROUNDED_NARROW_SCOPE",
                "classifier_id": batch.classifier_id,
                "labels": labels, "narrow_scope": None}
    scope = {"subject_key": parsed.subject_key,
             "predicate_key": parsed.predicate_key,
             "context_key": "general", "normalized_value": parsed.value_text}
    labels.update(scope)
    return {"status": "NARROW_SCOPE", "reason": "SOURCE_GROUNDED_NARROW_GRAMMAR",
            "classifier_id": batch.classifier_id,
            "labels": labels, "narrow_scope": scope}


def _confusion(rows):
    tp = sum(x["scope_emitted"] and x["draft_narrow_current_scope"] for x in rows)
    fp = sum(x["scope_emitted"] and not x["draft_narrow_current_scope"] for x in rows)
    fn = sum(not x["scope_emitted"] and x["draft_narrow_current_scope"] for x in rows)
    tn = sum(not x["scope_emitted"] and not x["draft_narrow_current_scope"] for x in rows)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp/(tp+fp) if tp+fp else None,
            "recall": tp/(tp+fn) if tp+fn else None,
            "specificity": tn/(tn+fp) if tn+fp else None}


def evaluate_corpus(path: Path, *, classifier=None) -> dict:
    rows, digest = load_corpus(path)
    cases = []
    for row in rows:
        prediction = extract_scope(row["text"], row["id"], classifier=classifier)
        labels = prediction["labels"]
        comparisons = {
            key: {
                "gold": row["draft"][key],
                "predicted": None if labels is None else labels[key],
                "match": (labels is not None and labels[key] is not None
                          and labels[key] == row["draft"][key]),
            }
            for key in DIMENSIONS
        }
        cases.append({
            "id": row["id"], "group": row["group"],
            "status": prediction["status"], "reason": prediction["reason"],
            "classifier_id": prediction["classifier_id"],
            "scope_emitted": prediction["narrow_scope"] is not None,
            "draft_narrow_current_scope": row["draft"]["narrow_current_scope"],
            "fields": comparisons,
        })
    by_group = {}
    for group in sorted({row["group"] for row in cases}):
        subset = [row for row in cases if row["group"] == group]
        by_group[group] = {
            "total": len(subset),
            "abstained": sum(row["status"] == "ABSTAIN" for row in subset),
            "semantic_only": sum(row["status"] == "SEMANTIC_ONLY" for row in subset),
            "scope_emitted": sum(row["scope_emitted"] for row in subset),
            "narrow_confusion": _confusion(subset),
        }
    metrics = {
        "total": len(cases),
        "stage_a_coverage": sum(row["status"] != "ABSTAIN" for row in cases),
        "scope_coverage": sum(row["scope_emitted"] for row in cases),
        "abstentions": dict(sorted(Counter(row["status"] for row in cases).items())),
        "narrow_confusion": _confusion(cases),
        "by_dimension": {
            key: {
                "supported": sum(row["fields"][key]["predicted"] is not None for row in cases),
                "correct": sum(row["fields"][key]["match"] for row in cases),
                "incorrect": sum(
                    row["fields"][key]["predicted"] is not None
                    and not row["fields"][key]["match"] for row in cases
                ),
                "abstained": sum(row["fields"][key]["predicted"] is None for row in cases),
            }
            for key in DIMENSIONS
        },
        "by_group": by_group,
        "false_scope_activations": [row["id"] for row in cases
                                    if row["scope_emitted"]
                                    and not row["draft_narrow_current_scope"]],
        "missed_supported_scope": [row["id"] for row in cases
                                   if not row["scope_emitted"]
                                   and row["draft_narrow_current_scope"]],
    }
    return {
        "version": "mvp02c13.semantic_scope_evaluation.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_version": DATASET_VERSION, "source_sha256": digest,
        "annotation_status": LABEL_STATUS,
        "classifier_name": "RuleSemanticClassifier" if classifier is None
                            else type(classifier).__name__,
        "metrics": metrics, "cases": cases,
        "limits": [
            "All labels are AUTHOR DRAFTS, NOT independently adjudicated gold.",
            "The synthetic corpus was constructed knowing the existing rule baseline.",
            "Scope extraction uses deterministic allowed grammar; no general model or Memory DB.",
            "This is not Memory conflict eligibility precision/recall and not deployment accuracy.",
            "Abstention is a safe unsupported output, not proof of semantic correctness.",
        ],
    }


def make_blind_template(path: Path) -> dict:
    rows, digest = load_corpus(path)
    return {
        "version": "mvp02c13.blind_review_template.v1",
        "source_sha256": digest,
        "reviewer_id": None,
        "instructions": "Independently annotate the full source text without draft gold, model outputs or diagnostic group tags. Replace reviewer_id and all null annotations. Do not infer Memory admission.",
        "annotations": [{
            "id": item["id"], "text": item["text"],
            "labels": {key: None for key in FIELDS},
        } for item in rows],
    }


def compare_reviews(path: Path, a_path: Path, b_path: Path) -> dict:
    rows, digest = load_corpus(path)
    expected = {row["id"] for row in rows}
    reviewers = []
    for review_file in (a_path, b_path):
        review, _ = read_json(review_file)
        if review.get("version") != "mvp02c13.blind_review_template.v1":
            raise ValueError("invalid review version")
        if review.get("source_sha256") != digest:
            raise ValueError("review source hash mismatch")
        reviewer_id = review.get("reviewer_id")
        if (not isinstance(reviewer_id, str) or
                re.fullmatch(r"[a-zA-Z0-9_-]{3,40}", reviewer_id) is None):
            raise ValueError("missing independent reviewer identifier")
        annotations = review.get("annotations")
        if not isinstance(annotations, list) or len(annotations) != len(rows):
            raise ValueError("reviewer must annotate every case")
        label_map = {}
        for item in annotations:
            if not isinstance(item, dict) or set(item) != {"id", "text", "labels"}:
                raise ValueError("unexpected review case fields")
            key = item["id"]
            source = next((row for row in rows if row["id"] == key), None)
            if source is None or key in label_map or item["text"] != source["text"]:
                raise ValueError("review has missing/duplicate/modified source case")
            check_labels(item["labels"])
            label_map[key] = item["labels"]
        if set(label_map) != expected:
            raise ValueError("review ID set mismatch")
        reviewers.append((reviewer_id, label_map))
    if reviewers[0][0] == reviewers[1][0]:
        raise ValueError("distinct reviewer identifiers required")
    conflicts = []
    field_matches = Counter()
    for case_id in sorted(expected):
        first, second = reviewers[0][1][case_id], reviewers[1][1][case_id]
        mismatched = [field for field in FIELDS if first[field] != second[field]]
        for field in FIELDS:
            field_matches[field] += field not in mismatched
        if mismatched:
            conflicts.append({"id": case_id, "different_fields": mismatched})
    return {
        "version": "mvp02c13.review_disagreement_report.v1",
        "source_sha256": digest,
        "reviewers": [pair[0] for pair in reviewers],
        "total": len(expected),
        "full_agreement": len(expected) - len(conflicts),
        "per_field_agreements": dict(field_matches),
        "disagreements": conflicts,
        "status": "REQUIRES_INDEPENDENT_ADJUDICATION",
        "warning": "Different reviewer IDs cannot prove independence; disagreements and unanimous labels require independent adjudication before replacing author draft labels.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c13_semantic_scope_corpus.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--blind-template", action="store_true")
    parser.add_argument("--review-a", type=Path)
    parser.add_argument("--review-b", type=Path)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite an existing evaluation artifact")
    if bool(args.review_a) != bool(args.review_b):
        parser.error("both review files are required")
    if args.blind_template and args.review_a:
        parser.error("blind-template and review comparison cannot be combined")
    try:
        if args.blind_template:
            data = make_blind_template(args.suite)
        elif args.review_a:
            data = compare_reviews(args.suite, args.review_a, args.review_b)
        else:
            data = evaluate_corpus(args.suite)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"Semantic scope evaluation failed: {type(exc).__name__}: {exc}\n")
    print(f"Saved {args.report}")
    if "metrics" in data:
        m = data["metrics"]
        print(f"{m['total']} cases: semantic={m['stage_a_coverage']} "
              f"scope={m['scope_coverage']} errors={len(m['false_scope_activations'])}, "
              f"missed={len(m['missed_supported_scope'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
