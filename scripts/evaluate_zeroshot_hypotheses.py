#!/usr/bin/env python3
"""MVP-02C.14C: paired offline zero-shot hypothesis refinement experiment.

Runs the *unchanged v14B hypotheses* and the frozen v14C hypotheses on
the same examples, model and process. No model-driven Memory decisions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import median
from time import perf_counter

from evaluate_semantic_scope import DATASET_VERSION, LABEL_STATUS, load_corpus
from evaluate_zeroshot_semantics import (
    HYPOTHESES as OLD_HYPOTHESES,
    HYPOTHESES_VERSION as OLD_VERSION,
    LocalTransformersZeroShot, ZeroShotBackend,
    _select, evaluate as evaluate_v14b, _write_once,
)

VERSION = "mvp02c14c.zeroshot_hypothesis_refinement.v1"
HYPOTHESES_VERSION = "mvp02c14c.hypotheses.v1"

# HYPOTHESES are frozen for this version and contain no UNKNOWN or
# UNSPECIFIED competing against affirmative semantic hypotheses.
# Semantic "not explicit" is not equivalent to "no current state."
REFINED = {
    "temporal_scope": {
        "CURRENT": "The speaker describes a preference or state that holds for them at the present time.",
        "HABITUAL": "The speaker describes a repeated action or regular habit in the present.",
        "PAST_EVENT": "The speaker reports a particular event that happened in the past.",
        "PAST_HABITUAL": "The speaker describes a past habit or past state that has ended.",
        "FUTURE": "The speaker describes a plan, event, or possibility in the future.",
    },
    "modality": {
        "ASSERTED": "The speaker presents the claim as something they believe is true.",
        "TENTATIVE": "The speaker expresses uncertainty or doubt about whether the claim is true.",
        "CONDITIONAL": "The claim applies only if a stated condition is satisfied.",
        "HYPOTHETICAL": "The speaker imagines a hypothetical situation, rather than claiming it is real.",
    },
    "correction_signal": {
        "CORRECTING": "The speaker is explicitly correcting or revising information that was given previously.",
        "NOT_CORRECTING": "The speaker makes a statement without explicitly correcting information given previously.",
    },
}
# "Unknown" author labels are not proof of the opposite class. They are
# excluded from paired agreement; never coerce them to a positive Memory claim.
GOLD_EXCLUDE = {
    "temporal_scope": frozenset(("UNSPECIFIED",)),
    "modality": frozenset(("UNKNOWN",)),
    "correction_signal": frozenset(("UNKNOWN",)),
}


def _gold(case: dict, dimension: str) -> str:
    if dimension == "correction_signal":
        label = case["draft"]["pragmatic_function"]
        return "CORRECTING" if label == "CORRECTING" else (
            "UNKNOWN" if label == "UNKNOWN" else "NOT_CORRECTING"
        )
    return case["draft"][dimension]


def _reference(case: dict, dimension: str) -> dict:
    source = ("pragmatic_function" if dimension == "correction_signal"
              else dimension)
    original = case["zero_shot"][source]
    label = original["prediction"]
    if dimension == "correction_signal":
        # v14B UNKNOWN is an uninformative top-class, not evidence
        # that no correction occurred.
        if label == "CORRECTING":
            label = "CORRECTING"
        elif label in ("INFORMING", "REQUESTING", "ASKING"):
            label = "NOT_CORRECTING"
        else:
            label = None
    elif label in ("UNKNOWN", "UNSPECIFIED"):
        label = None
    return {"prediction": label, "status": original["status"],
            "top_score": original["top_score"],
            "margin": original["margin"]}


def _score(cases: list[dict], dimension: str, which: str) -> dict:
    relevant = [
        c for c in cases if c["gold"][dimension] not in GOLD_EXCLUDE[dimension]
    ]
    def get(c):
        return c[which][dimension]["prediction"]
    n = len(relevant)
    supported = sum(get(c) is not None for c in relevant)
    correct = sum(get(c) is not None and get(c) == c["gold"][dimension]
                  for c in relevant)
    labels = tuple(REFINED[dimension])
    by_label = {}
    for label in labels:
        tp = sum(get(c) == label and c["gold"][dimension] == label
                 for c in relevant)
        fp = sum(get(c) == label and c["gold"][dimension] != label
                 for c in relevant)
        fn = sum(get(c) != label and c["gold"][dimension] == label
                 for c in relevant)
        by_label[label] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(tp / (tp + fp), 6) if tp + fp else None,
            "recall": round(tp / (tp + fn), 6) if tp + fn else None,
        }
    return {
        "eligible": n, "unscored_ambiguous_drafts": len(cases) - n,
        "supported": supported, "correct": correct,
        "incorrect": supported - correct, "abstained": n - supported,
        "errors": sum(c[which][dimension]["status"] == "MODEL_ERROR"
                      for c in relevant),
        "agreement_on_eligible": round(correct / n, 6) if n else None,
        "conditional_agreement": round(correct / supported, 6) if supported else None,
        "coverage_on_eligible": round(supported / n, 6) if n else None,
        "by_label": by_label,
    }


def evaluate(
    rows: list[dict], *, backend: ZeroShotBackend, batch_size: int = 4,
    min_score: float = 0.0, min_margin: float = 0.0,
) -> dict:
    """Benchmark v14B and v14C on the identical model/data in one run.

    The source text is sent to the classifier only. Draft labels/group
    tags are used solely in evaluator code after prediction.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise ValueError("batch-size must be 1..32")
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1
           for x in (min_score, min_margin)):
        raise ValueError("thresholds must be in [0,1] and finite")

    previous = evaluate_v14b(
        rows, backend=backend, batch_size=batch_size,
        min_score=min_score, min_margin=min_margin,
    )
    previous_by_id = {item["id"]: item for item in previous["cases"]}
    cases = [{
        "id": row["id"], "group": row["group"],
        "gold": {dimension: _gold(row, dimension) for dimension in REFINED},
        "reference": {
            dimension: _reference(previous_by_id[row["id"]], dimension)
            for dimension in REFINED
        },
        "refined": {},
    } for row in rows]
    batches = []
    for dimension, hypotheses in REFINED.items():
        phrases = list(hypotheses.values())
        for begin in range(0, len(rows), batch_size):
            subset = rows[begin:begin + batch_size]
            started = perf_counter()
            try:
                predictions = backend.classify(
                    [row["text"] for row in subset], phrases
                )
                if not isinstance(predictions, list) or len(predictions) != len(subset):
                    raise ValueError("model batch length mismatch")
                results = [
                    _select(p, hypotheses, min_score=min_score, min_margin=min_margin)
                    for p in predictions
                ]
                status, reason = "OK", None
            except Exception as exc:
                # Do not serialize model errors or source-bearing exception strings.
                results = [{
                    "status": "MODEL_ERROR", "reason": type(exc).__name__,
                    "prediction": None, "top_label": None, "top_score": None,
                    "margin": None, "scores": None,
                } for _ in subset]
                status, reason = "ERROR", type(exc).__name__
            elapsed = round((perf_counter() - started) * 1000, 3)
            batches.append({
                "dimension": dimension, "size": len(subset),
                "elapsed_ms": elapsed, "status": status, "reason": reason,
            })
            for index, prediction in enumerate(results, start=begin):
                cases[index]["refined"][dimension] = prediction

    for case in cases:
        for dimension in REFINED:
            gold = case["gold"][dimension]
            for which in ("reference", "refined"):
                case[which][dimension]["matches_draft"] = (
                    gold not in GOLD_EXCLUDE[dimension]
                    and case[which][dimension]["prediction"] is not None
                    and case[which][dimension]["prediction"] == gold
                )
    by_dimension = {
        dimension: {
            "v14b": _score(cases, dimension, "reference"),
            "v14c": _score(cases, dimension, "refined"),
        } for dimension in REFINED
    }
    groups = {
        group: {
            dim: {
                "v14b": _score(subset, dim, "reference"),
                "v14c": _score(subset, dim, "refined"),
            } for dim in REFINED
        }
        for group in sorted({case["group"] for case in cases})
        for subset in ([case for case in cases if case["group"] == group],)
    }
    correct_gain = {
        dim: {
            "newly_correct": [case["id"] for case in cases
                              if case["refined"][dim]["matches_draft"]
                              and not case["reference"][dim]["matches_draft"]],
            "regressed": [case["id"] for case in cases
                          if case["reference"][dim]["matches_draft"]
                          and not case["refined"][dim]["matches_draft"]],
        } for dim in REFINED
    }
    return {
        "total": len(cases),
        "v14b_version": OLD_VERSION, "v14b_reference": {
            "by_dimension": previous["by_dimension"],
            "batch_calls": previous["batch_calls"],
            "error_batches": previous["error_batches"],
            "total_inference_ms": previous["total_inference_ms"],
        },
        "refined_batch_calls": len(batches),
        "refined_error_batches": sum(b["status"] == "ERROR" for b in batches),
        "refined_inference_ms": round(sum(b["elapsed_ms"] for b in batches), 3),
        "median_refined_batch_ms": median(b["elapsed_ms"] for b in batches),
        "by_dimension": by_dimension, "by_group": groups,
        "changes": correct_gain, "refined_batches": batches, "cases": cases,
    }


def main(argv=None, *, backend: ZeroShotBackend | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c13_semantic_scope_corpus.json"
    ))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--min-margin", type=float, default=0.0)
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite existing report")
    try:
        rows, digest = load_corpus(args.suite)
        if args.case_ids:
            chosen = set(args.case_ids)
            if len(chosen) != len(args.case_ids) or not chosen.issubset(
                {row["id"] for row in rows}
            ):
                raise ValueError("unknown or duplicate case ID")
            rows = [row for row in rows if row["id"] in chosen]
        started = perf_counter()
        gateway = backend if backend is not None else LocalTransformersZeroShot(
            args.model_dir, device=args.device
        )
        model_load_ms = round((perf_counter() - started) * 1000, 3)
        outcome = evaluate(
            rows, backend=gateway, batch_size=args.batch_size,
            min_score=args.min_score, min_margin=args.min_margin,
        )
        report = {
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_version": DATASET_VERSION, "dataset_sha256": digest,
            "annotation_status": LABEL_STATUS,
            "v14b_hypotheses_version": OLD_VERSION,
            "v14b_hypotheses_sha256": sha256(
                json.dumps(OLD_HYPOTHESES, sort_keys=True).encode()
            ).hexdigest(),
            "v14c_hypotheses_version": HYPOTHESES_VERSION,
            "v14c_hypotheses_sha256": sha256(
                json.dumps(REFINED, sort_keys=True).encode()
            ).hexdigest(),
            "model_id": args.model_dir.expanduser().name,
            "device": args.device, "batch_size": args.batch_size,
            "min_score": args.min_score, "min_margin": args.min_margin,
            "full_corpus": not bool(args.case_ids),
            "model_load_ms": model_load_ms,
            "metrics": outcome,
            "limitations": [
                "Dataset labels are provisional author drafts, not independent adjudications.",
                "Paired hypotheses tested on the same source/model; hypothesis wording is experimental.",
                "v14B UNKNOWN/UNSPECIFIED reference predictions map to abstention in paired scoring.",
                "Ambiguous gold labels excluded from comparable per-dimension denominators.",
                "Classifier confidence is uncalibrated; top-1 classification does not cover mixed claims.",
                "Binary NOT_CORRECTING is not proof that no correction exists.",
                "No canonical subject/predicate, Memory scope, mutation, or production routing.",
                "No model downloads and no input text or raw model responses in the report.",
                "A new independent holdout is required before model/threshold selection.",
            ],
        }
        _write_once(args.report, report)
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Zero-shot refinement failed: {type(exc).__name__}: {exc}\n")
    print(f"{len(rows)} cases; v14B comparison errors "
          f"{outcome['v14b_reference']['error_batches']}; refined errors "
          f"{outcome['refined_error_batches']}; saved {args.report}")
    return int(
        bool(outcome["v14b_reference"]["error_batches"]
             or outcome["refined_error_batches"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
