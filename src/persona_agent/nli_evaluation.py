"""Pure-Python, offline evaluation of NLI models on reviewed sentence pairs.

These are diagnostic labels, not approved persona memories and not a published
benchmark. No ML runtime dependency or database access is required here.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Mapping

LABELS = ("contradiction", "entailment", "neutral")


@dataclass(frozen=True)
class NLIPair:
    id: str
    label: str
    group: str
    premise: str
    hypothesis: str


def load_nli_suite(path: Path) -> tuple[list[NLIPair], str]:
    """Validate a bounded, deterministic input suite and return its SHA256."""
    raw = Path(path).read_bytes()
    if len(raw) > 200_000:
        raise ValueError("NLI suite is too large")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("version") != "mvp02c5.synthetic_nli.v1":
        raise ValueError("unsupported NLI suite version")
    examples = document.get("examples")
    if not isinstance(examples, list) or not 3 <= len(examples) <= 200:
        raise ValueError("NLI suite must have 3..200 examples")
    pairs = []
    seen: set[str] = set()
    for example in examples:
        if not isinstance(example, dict) or set(example) != {
            "id", "label", "group", "premise", "hypothesis"
        }:
            raise ValueError("NLI example fields are invalid")
        if any(not isinstance(example[key], str) for key in example):
            raise ValueError("NLI example must contain strings")
        if not (1 <= len(example["id"]) <= 32) or not example["id"].isascii():
            raise ValueError("invalid NLI example ID")
        if example["id"] in seen:
            raise ValueError("duplicate NLI example ID")
        seen.add(example["id"])
        if example["label"] not in LABELS:
            raise ValueError("unrecognized NLI label")
        if not (1 <= len(example["group"]) <= 48):
            raise ValueError("invalid NLI group")
        if not all(
            3 <= len(example[key].strip()) <= 500
            for key in ("premise", "hypothesis")
        ):
            raise ValueError("NLI sentence length out of range")
        pairs.append(NLIPair(**example))
    if set(p.label for p in pairs) != set(LABELS):
        raise ValueError("NLI suite must contain all three labels")
    return pairs, sha256(raw).hexdigest()


def summarize_nli(
    examples: list[NLIPair],
    outputs: Mapping[str, Mapping[str, float]],
    *,
    high_confidence_threshold: float = 0.9,
) -> dict:
    """Confusion, per-label precision/recall, high-confidence false alerts."""
    if not 0 < high_confidence_threshold < 1:
        raise ValueError("invalid high-confidence threshold")
    if set(outputs) != {pair.id for pair in examples}:
        raise ValueError("NLI predictions do not match suite IDs")
    confusion = {
        label: {predicted: 0 for predicted in LABELS} for label in LABELS
    }
    per_group: dict[str, dict[str, int]] = {}
    cases = []
    false_contradictions = 0
    high_confidence_false_contradictions = 0
    threshold_tp = threshold_fp = threshold_fn = threshold_tn = 0
    for pair in examples:
        probabilities = outputs[pair.id]
        if set(probabilities) != set(LABELS):
            raise ValueError("NLI prediction labels mismatch")
        scores = {}
        for name, value in probabilities.items():
            if type(value) not in (float, int) or not isfinite(value) or not 0 <= value <= 1:
                raise ValueError("non-finite or out-of-range NLI probability")
            scores[name] = float(value)
        if not 0.999 <= sum(scores.values()) <= 1.001:
            raise ValueError("NLI probabilities are not normalized")
        predicted = max(LABELS, key=lambda label: scores[label])
        confusion[pair.label][predicted] += 1
        group = per_group.setdefault(pair.group, {"correct": 0, "total": 0})
        group["total"] += 1
        group["correct"] += int(predicted == pair.label)
        is_false_contradiction = pair.label != "contradiction" and predicted == "contradiction"
        false_contradictions += int(is_false_contradiction)
        high_confidence_false_contradictions += int(
            is_false_contradiction and scores["contradiction"] >= high_confidence_threshold
        )
        threshold_hit = scores["contradiction"] >= high_confidence_threshold
        expected_contradiction = pair.label == "contradiction"
        threshold_tp += int(threshold_hit and expected_contradiction)
        threshold_fp += int(threshold_hit and not expected_contradiction)
        threshold_fn += int(not threshold_hit and expected_contradiction)
        threshold_tn += int(not threshold_hit and not expected_contradiction)
        threshold_outcome = ("TP" if expected_contradiction else "FP") if threshold_hit else (
            "FN" if expected_contradiction else "TN"
        )
        cases.append({
            "contradiction_at_threshold": threshold_hit,
            "contradiction_threshold_outcome": threshold_outcome,
            "id": pair.id, "group": pair.group,
            "expected": pair.label, "predicted": predicted,
            "correct": predicted == pair.label,
            "scores": {key: round(scores[key], 6) for key in LABELS},
            # Include the synthetic evidence used by the diagnostic for review.
            "premise": pair.premise, "hypothesis": pair.hypothesis,
        })
    per_label = {}
    for label in LABELS:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        predicted_count = sum(confusion[gold][label] for gold in LABELS)
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else 0.0
        per_label[label] = {
            "support": support,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(2*precision*recall/(precision+recall), 4)
            if precision+recall else 0.0,
        }
    correct = sum(pair["correct"] for pair in cases)
    negative_support = sum(pair.label != "contradiction" for pair in examples)
    threshold_precision = threshold_tp / (threshold_tp + threshold_fp) if threshold_tp + threshold_fp else 0.0
    threshold_recall = threshold_tp / (threshold_tp + threshold_fn) if threshold_tp + threshold_fn else 0.0
    return {
        "contradiction_threshold": {
            "score_cutoff": high_confidence_threshold,
            "true_positive": threshold_tp,
            "false_positive": threshold_fp,
            "false_negative": threshold_fn,
            "true_negative": threshold_tn,
            "precision": round(threshold_precision, 4),
            "recall": round(threshold_recall, 4),
            "flagged_total": threshold_tp + threshold_fp,
            "false_alert_rate_noncontradiction": round(
                threshold_fp / (threshold_fp + threshold_tn), 4
            ) if threshold_fp + threshold_tn else 0.0,
        },
        "total": len(examples),
        "correct": correct,
        "accuracy": round(correct / len(examples), 4),
        "confusion_gold_rows": confusion,
        "per_label": per_label,
        "per_group": {
            group: {**counts, "accuracy": round(counts["correct"]/counts["total"], 4)}
            for group, counts in sorted(per_group.items())
        },
        "false_contradiction_count": false_contradictions,
        "false_contradiction_rate_noncontradiction": round(
            false_contradictions/negative_support, 4
        ) if negative_support else 0,
        "high_confidence_false_contradictions": high_confidence_false_contradictions,
        "high_confidence_threshold": high_confidence_threshold,
        "cases": cases,
    }


def compare_nli_runs(runs: list[dict]) -> list[dict]:
    """Compare variants on exactly the same synthetic input IDs."""
    differences = []
    for index, left in enumerate(runs):
        lc = {c["id"]: c for c in left["metrics"]["cases"]}
        for right in runs[index+1:]:
            rc = {c["id"]: c for c in right["metrics"]["cases"]}
            if lc.keys() != rc.keys():
                raise ValueError("cannot compare NLI runs with different cases")
            changed = []
            left_better = []
            right_better = []
            for cid in lc:
                a, b = lc[cid], rc[cid]
                if a["predicted"] != b["predicted"]:
                    changed.append(cid)
                if a["correct"] and not b["correct"]:
                    left_better.append(cid)
                if b["correct"] and not a["correct"]:
                    right_better.append(cid)
            differences.append({
                "left": left["key"], "right": right["key"],
                "prediction_disagreements": changed,
                "left_only_correct": left_better,
                "right_only_correct": right_better,
                "contradiction_false_alert_delta_right_minus_left":
                    right["metrics"]["false_contradiction_count"] -
                    left["metrics"]["false_contradiction_count"],
            })
    return differences
