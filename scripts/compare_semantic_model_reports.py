#!/usr/bin/env python3
"""MVP-02C.14E compare already-produced LOCAL span-conditioned probe reports.

Never reopens source text or claims model accuracy. Paired comparison requires
identical suite bytes, prompt version, case selection and reference claim count.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from evaluate_semantic_models import LABEL_FIELDS, PROMPT_VERSION, VERSION

COMPARISON_VERSION = "mvp02c14e.paired_probe_comparison.v1"
REFERENCE_MODE = "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY"
ANNOTATION_STATUS = "AUTHOR_DRAFT_NOT_INDEPENDENT"


def _fraction(num: int, den: int) -> float | None:
    return round(num / den, 6) if den else None


def compare_reports(paths: list[Path]) -> dict:
    if not 2 <= len(paths) <= 5:
        raise ValueError("expected two to five distinct reports")
    if len({str(path.resolve()) for path in paths}) != len(paths):
        raise ValueError("duplicate report path")
    loaded = []
    for path in paths:
        report = json.loads(path.read_bytes())
        if not isinstance(report, dict):
            raise ValueError("invalid report")
        if (report.get("version") != VERSION
                or report.get("prompt_version") != PROMPT_VERSION
                or report.get("reference_mode") != REFERENCE_MODE
                or report.get("annotation_status") != ANNOTATION_STATUS):
            raise ValueError("unexpected model report version or provenance")
        if not isinstance(report.get("by_dimension"), dict):
            raise ValueError("missing dimension results")
        if set(report["by_dimension"]) != set(LABEL_FIELDS):
            raise ValueError("inconsistent model dimension fields")
        loaded.append(report)

    fingerprint = lambda r: (
        r["suite_sha256"], r["prompt_version"],
        tuple((row["id"], row["reference_claims"]) for row in r["cases"]),
        r["sample_cases"], r["sample_claims"],
    )
    if any(fingerprint(row) != fingerprint(loaded[0]) for row in loaded[1:]):
        raise ValueError("reports are not paired on identical cases and claims")
    if len({r["backend"] for r in loaded}) != len(loaded):
        raise ValueError("duplicate model backend identity")
    backend_results = []
    for report in loaded:
        dimensions = {}
        for field in LABEL_FIELDS:
            metrics = report["by_dimension"][field]
            eligible = metrics["eligible"]
            supported = metrics["supported"]
            correct = metrics["correct"]
            incorrect = metrics["incorrect"]
            abstained = metrics["abstained"]
            if (not all(type(n) is int and n >= 0
                        for n in (eligible, supported, correct, incorrect, abstained))
                    or correct + incorrect != supported
                    or supported + abstained != eligible):
                raise ValueError("inconsistent dimension metrics")
            dimensions[field] = {
                "eligible": eligible, "supported": supported,
                "correct": correct, "incorrect": incorrect,
                "abstained": abstained,
                "coverage": _fraction(supported, eligible),
                "conditional_draft_agreement": _fraction(correct, supported),
                "overall_draft_agreement": _fraction(correct, eligible),
            }
        backend_results.append({
            "backend": report["backend"],
            "error_claims": report["error_claims"],
            "median_claim_inference_ms": report["median_claim_inference_ms"],
            "total_inference_ms": report["total_inference_ms"],
            "dimensions": dimensions,
        })
    return {
        "version": COMPARISON_VERSION,
        "reference_mode": REFERENCE_MODE,
        "annotation_status": ANNOTATION_STATUS,
        "prompt_version": loaded[0]["prompt_version"],
        "suite_sha256": loaded[0]["suite_sha256"],
        "paired_case_ids": [row["id"] for row in loaded[0]["cases"]],
        "paired_claims": loaded[0]["sample_claims"],
        "backends": backend_results,
        "limits": [
            "Author-draft diagnostic agreement, NOT model accuracy.",
            "Oracle reference claim spans; no extraction, qualifier or relation scores.",
            "Do not rank models without independently adjudicated holdout.",
            "Cold model load excluded from inference latency.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite existing comparison")
    try:
        result = compare_reports(args.reports)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        parser.error("invalid, missing, or non-comparable reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
