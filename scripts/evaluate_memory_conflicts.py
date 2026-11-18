#!/usr/bin/env python3
"""Offline, memory-specific NLI diagnostic; no persona/Memory integration.

Runs already-cached ONNX FP32 and/or INT8 checkpoints on draft, synthetic
memory-claim pairs. 'Oracle' eligibility is a human annotation, NOT a
production eligibility classifier or authorization to change memory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil, isfinite
import os
from pathlib import Path
import statistics

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from persona_agent.local_onnx import (
    LocalONNXConfig, LocalONNXModel, select_onnx_file,
)

VERSION = "mvp02c10.memory_conflict_diagnostic.v1"
THRESHOLD = 0.90  # Predeclared, uncalibrated diagnostic mark; not memory policy.
LABELS = frozenset(("entailment", "neutral", "contradiction"))
REASONS = frozenset((
    "SAME_SUBJECT_SCOPE_ASSERTED", "INDEPENDENT_PREDICATE",
    "DIFFERENT_TIME", "INTENTION_NOT_OUTCOME", "NON_ASSERTED_HYPOTHETICAL",
    "NON_SELF_REPORT_OR_QUOTED", "DIFFERENT_SUBJECT_OR_CONTEXT",
    "UNCERTAIN_OR_TENTATIVE",
))
FIELDS = frozenset((
    "id", "group", "stored_text", "candidate_text", "expected_nli",
    "review_applicable", "eligibility_reason",
))


def load_suite(path: Path) -> tuple[list[dict], str]:
    """Strict, bounded, versioned dataset; no silently corrected gold labels."""
    raw = Path(path).read_bytes()
    if len(raw) > 100_000:
        raise ValueError("memory diagnostic suite too large")
    obj = json.loads(raw)
    if not isinstance(obj, dict) or obj.get("version") != VERSION:
        raise ValueError("unsupported memory diagnostic suite version")
    if obj.get("annotation_status") != "DRAFT_UNREVIEWED":
        raise ValueError("unexpected annotation status; review requires new dataset version")
    items = obj.get("examples")
    if not isinstance(items, list) or not 3 <= len(items) <= 200:
        raise ValueError("memory diagnostic requires 3..200 examples")
    ids = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != FIELDS:
            raise ValueError("invalid example fields")
        key = item["id"]
        if not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 32 or key in ids:
            raise ValueError("invalid or duplicate example ID")
        ids.add(key)
        if not isinstance(item["group"], str) or not 1 <= len(item["group"]) <= 48:
            raise ValueError("invalid example group")
        for field in ("stored_text", "candidate_text"):
            value = item[field]
            if not isinstance(value, str) or not 3 <= len(value.strip()) <= 500:
                raise ValueError("invalid example text")
        if item["expected_nli"] not in LABELS:
            raise ValueError("unrecognized NLI gold label")
        if type(item["review_applicable"]) is not bool:
            raise ValueError("review_applicable must be bool")
        reason = item["eligibility_reason"]
        if reason not in REASONS:
            raise ValueError("unrecognized eligibility reason")
        if item["review_applicable"] != (reason == "SAME_SUBJECT_SCOPE_ASSERTED"):
            raise ValueError("eligibility annotation inconsistent with reason")
    if {item["expected_nli"] for item in items} != LABELS:
        raise ValueError("suite must cover all NLI labels")
    return items, sha256(raw).hexdigest()


def _counts(flags: list[tuple[bool, bool]]) -> dict:
    tp = sum(p and g for p, g in flags)
    fp = sum(p and not g for p, g in flags)
    fn = sum(not p and g for p, g in flags)
    tn = sum(not p and not g for p, g in flags)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
        "false_alert_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
    }


def score_cases(cases: list[dict], predictions: dict[str, dict[str, float]]) -> dict:
    """Separate textual NLI errors from memory-review false alarms.

    'oracle-filtered' scores assume PERFECT externally supplied eligibility
    annotations, so they are only a hypothetical upper-bound diagnostic.
    """
    if set(predictions) != {case["id"] for case in cases}:
        raise ValueError("predictions do not match suite case IDs")
    rows = []
    for case in cases:
        scores = predictions[case["id"]]
        if not isinstance(scores, dict) or set(scores) != LABELS:
            raise ValueError("unexpected NLI score labels")
        if any(type(v) not in (float, int) or not isfinite(v) or not 0 <= v <= 1
               for v in scores.values()) or abs(sum(scores.values()) - 1) > .001:
            raise ValueError("NLI scores must be finite normalized probabilities")
        pred = max(sorted(LABELS), key=lambda label: scores[label])
        raw_alert = scores["contradiction"] >= THRESHOLD
        eligible = case["review_applicable"]
        expected_review = eligible and case["expected_nli"] == "contradiction"
        rows.append({
            "id": case["id"], "group": case["group"],
            "expected_nli": case["expected_nli"], "predicted_nli": pred,
            "review_applicable": eligible, "eligibility_reason": case["eligibility_reason"],
            "expected_review": expected_review,
            "raw_alert": raw_alert,
            "oracle_filtered_alert": raw_alert and eligible,
            "scores": {label: round(float(scores[label]), 8) for label in sorted(LABELS)},
        })
    def summarize(subset: list[dict]) -> dict:
        raw = _counts([(r["raw_alert"], r["expected_review"]) for r in subset])
        oracle = _counts([(r["oracle_filtered_alert"], r["expected_review"]) for r in subset])
        return {
            "total": len(subset),
            "nli_argmax_correct": sum(r["expected_nli"] == r["predicted_nli"] for r in subset),
            "raw": raw,
            "oracle_eligibility_filter": oracle,
            "raw_false_alert_ids": [r["id"] for r in subset if r["raw_alert"] and not r["expected_review"]],
            "oracle_false_alert_ids": [r["id"] for r in subset if r["oracle_filtered_alert"] and not r["expected_review"]],
        }
    groups = sorted({case["group"] for case in cases})
    return {
        "summary": summarize(rows),
        "by_group": {g: summarize([r for r in rows if r["group"] == g]) for g in groups},
        "cases": rows,
    }


def _p95(values: list[float]) -> float:
    return sorted(values)[ceil(len(values) * .95) - 1]


def evaluate_variant(cases: list[dict], model_dir: Path, precision: str,
                     threads: int, repetitions: int, warmup: int,
                     model_factory=LocalONNXModel) -> dict:
    from time import perf_counter

    start = perf_counter()
    model = model_factory(LocalONNXConfig(model_dir=model_dir, role="nli",
                                         precision=precision, intra_op_threads=threads))
    load_ms = (perf_counter() - start) * 1000
    if model.labels != {"entailment": 0, "neutral": 1, "contradiction": 2}:
        raise ValueError("FineCat NLI label order does not match reviewed config")
    for _ in range(warmup):
        model.nli(cases[0]["stored_text"], cases[0]["candidate_text"])
    predictions = {}
    elapsed = []
    for _ in range(repetitions):
        for case in cases:
            start = perf_counter()
            predictions[case["id"]] = model.nli(
                case["stored_text"], case["candidate_text"])
            elapsed.append((perf_counter() - start) * 1000)
    metrics = score_cases(cases, predictions)
    return {
        "precision": precision,
        "model_file": str(model.model_file),
        "model_bytes_graph_only": Path(model.model_file).stat().st_size,
        "onnxruntime_version": model.ort_version,
        "load_ms": round(load_ms, 3),
        "median_pair_ms": round(statistics.median(elapsed), 3),
        "p95_pair_ms": round(_p95(elapsed), 3),
        "repetitions": repetitions,
        **metrics,
    }


def compare_variants(first: dict, second: dict) -> dict:
    a = {row["id"]: row for row in first["cases"]}
    b = {row["id"]: row for row in second["cases"]}
    if a.keys() != b.keys():
        raise ValueError("variant case IDs mismatch")
    return {
        "from": first["precision"], "to": second["precision"],
        "argmax_changes": [key for key in a if a[key]["predicted_nli"] != b[key]["predicted_nli"]],
        "raw_threshold_changes": [key for key in a if a[key]["raw_alert"] != b[key]["raw_alert"]],
        "oracle_filter_changes": [key for key in a if a[key]["oracle_filtered_alert"] != b[key]["oracle_filtered_alert"]],
        "max_contradiction_score_delta": round(max(
            abs(a[key]["scores"]["contradiction"] - b[key]["scores"]["contradiction"])
            for key in a), 8),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path,
                        default=Path("scenarios/mvp02c10_memory_conflict_diagnostic.json"))
    parser.add_argument("--model-dir", type=Path, default=Path(
                        "~/models/nli/finecat-nli-v2-xl").expanduser())
    parser.add_argument("--precisions", nargs="+", choices=("fp32", "int8"),
                        default=["fp32", "int8"])
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if len(args.precisions) != len(set(args.precisions)):
        parser.error("duplicate precisions")
    if not 1 <= args.threads <= 32 or not 1 <= args.repetitions <= 20 or not 0 <= args.warmup <= 10:
        parser.error("invalid threads/repetitions/warmup")
    if args.report.exists():
        parser.error("refusing to overwrite existing report")
    try:
        cases, digest = load_suite(args.suite)
        model_dir = args.model_dir.expanduser().resolve()
        if model_dir.name != "finecat-nli-v2-xl":
            raise ValueError("this diagnostic accepts only the reviewed FineCat checkpoint")
        if not (model_dir / "config.json").is_file():
            raise FileNotFoundError("missing locally cached FineCat config.json")
        for precision in args.precisions:
            select_onnx_file(model_dir, precision=precision)
        runs = [evaluate_variant(cases, model_dir, precision, args.threads,
                                 args.repetitions, args.warmup)
                for precision in args.precisions]
        report = {
            "version": "mvp02c10.memory_conflict_report.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "suite_path": str(args.suite), "suite_sha256": digest,
            "annotation_status": "DRAFT_UNREVIEWED",
            "model": model_dir.name,
            "model_config_sha256": sha256((model_dir / "config.json").read_bytes()).hexdigest(),
            "threshold": THRESHOLD, "threads": args.threads,
            "runs": runs,
            "variant_comparison": compare_variants(runs[0], runs[1]) if len(runs) == 2 else None,
            "caveats": [
                "All labels are draft; independent human review is required.",
                "Synthetic data is not a real-conversation or deployment accuracy estimate.",
                "Oracle eligibility is annotated ground truth, NOT an implemented candidate gate.",
                "Softmax scores are uncalibrated; 0.90 is diagnostic, NOT production policy.",
                "This offline tool is read-only and cannot authorize a Memory domain change.",
            ],
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2, ensure_ascii=False)
            target.write("\n")
        for run in runs:
            overall = run["summary"]
            print(f"{run['precision']}: NLI {overall['nli_argmax_correct']}/{overall['total']}; "
                  f"raw TP/FP/FN={overall['raw']['tp']}/{overall['raw']['fp']}/{overall['raw']['fn']}; "
                  f"oracle TP/FP/FN={overall['oracle_eligibility_filter']['tp']}/"
                  f"{overall['oracle_eligibility_filter']['fp']}/"
                  f"{overall['oracle_eligibility_filter']['fn']}; "
                  f"median={run['median_pair_ms']}ms")
        print(f"Saved {args.report}")
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError) as exc:
        parser.exit(2, f"Memory NLI evaluation failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
