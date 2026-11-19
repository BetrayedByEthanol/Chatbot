#!/usr/bin/env python3
"""MVP-02C.14G: review v14F paired real-model evidence without selecting a model.

Only consumes text-free score/profile JSON. Invalid/unreliable model output
is NOT semantic disagreement; author drafts are NOT independent gold.
No production routing, memory admission, retraining, or model execution.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from evaluate_semantic_models import LABEL_FIELDS, PROMPT_VERSION, VERSION as SCORE_VERSION
from benchmark_semantic_models import VERSION as PROFILE_VERSION

VERSION = "mvp02c14g.benchmark_analysis.v1"
REFERENCE_MODE = "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY"
ANNOTATION_STATUS = "AUTHOR_DRAFT_NOT_INDEPENDENT"
MIN_REVIEWED_ELIGIBLE_PROPOSAL = 200
MAX_ERROR_FRACTION_PROPOSAL = .01
MIN_COVERAGE_PROPOSAL = .80
MIN_CONDITIONAL_AGREEMENT_PROPOSAL = .95


def _load(path: Path) -> tuple[dict, str]:
    data = path.read_bytes()
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result, sha256(data).hexdigest()


def _number(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _counts(record: dict, *, field: str, claim_count: int):
    if not isinstance(record, dict):
        raise ValueError("invalid dimension metrics")
    keys = ("eligible", "supported", "correct", "incorrect", "abstained",
            "unscored_reference")
    values = {key: _number(record[key], key) for key in keys}
    if values["correct"] + values["incorrect"] != values["supported"]:
        raise ValueError("inconsistent supported prediction counts")
    if values["supported"] + values["abstained"] != values["eligible"]:
        raise ValueError("inconsistent eligible counts")
    if values["eligible"] + values["unscored_reference"] != claim_count:
        raise ValueError(f"inconsistent reference labels: {field}")
    return values


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _load_pair(score_path: Path, profile_path: Path) -> dict:
    score, score_sha = _load(score_path)
    profile, profile_sha = _load(profile_path)
    for doc, version in ((score, SCORE_VERSION), (profile, PROFILE_VERSION)):
        if (doc.get("version") != version
                or doc.get("reference_mode") != REFERENCE_MODE
                or doc.get("annotation_status") != ANNOTATION_STATUS
                or doc.get("prompt_version") != PROMPT_VERSION):
            raise ValueError("unexpected benchmark contract or provenance")
    if not isinstance(profile.get("backend"), str) or not profile["backend"]:
        raise ValueError("missing backend identity")
    for field in ("backend", "suite_sha256", "sample_cases", "sample_claims"):
        if score.get(field) != profile.get(field):
            raise ValueError("score/profile provenance mismatch")
    count = _number(score["sample_claims"], "sample_claims")
    sample_cases = _number(score["sample_cases"], "sample_cases")
    if not count or not sample_cases:
        raise ValueError("empty benchmark")
    cases = score.get("cases")
    if not isinstance(cases, list) or len(cases) != sample_cases:
        raise ValueError("invalid score case list")
    ids = []
    total = 0
    for item in cases:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("invalid case ID")
        ids.append(item["id"])
        total += _number(item["reference_claims"], "reference_claims")
        if _number(item["errors"], "case errors") > item["reference_claims"]:
            raise ValueError("case errors exceed claims")
    if len(set(ids)) != len(ids) or total != count:
        raise ValueError("duplicate case IDs or claim count mismatch")
    if profile.get("case_ids") != ids:
        raise ValueError("case selection mismatch")
    if sum(item["errors"] for item in cases) != score.get("error_claims"):
        raise ValueError("score error totals disagree")
    if not isinstance(profile.get("runs"), list) or not profile["runs"]:
        raise ValueError("missing measured repetitions")
    if not isinstance(score.get("by_dimension"), dict) or (
        set(score["by_dimension"]) != set(LABEL_FIELDS)
    ):
        raise ValueError("unexpected score dimensions")
    for idx, run in enumerate(profile["runs"]):
        if run.get("index") != idx + 1:
            raise ValueError("unordered repetitions")
        errors = _number(run["error_claims"], "run errors")
        if errors > count:
            raise ValueError("errors exceed claim count")
        if set(run["by_dimension"]) != set(LABEL_FIELDS):
            raise ValueError("unexpected run dimensions")
        for dim in LABEL_FIELDS:
            _counts(run["by_dimension"][dim], field=dim, claim_count=count)
    for dim in LABEL_FIELDS:
        _counts(score["by_dimension"][dim], field=dim, claim_count=count)
        if score["by_dimension"][dim] != profile["runs"][0]["by_dimension"][dim]:
            raise ValueError("score report differs from measured first run")
    if score["error_claims"] != profile["runs"][0]["error_claims"]:
        raise ValueError("first-run error counts differ")

    return {
        "score": score, "profile": profile,
        "score_sha256": score_sha, "profile_sha256": profile_sha,
        "case_ids": ids,
    }


def analyze_pairs(paths: list[tuple[Path, Path]], *, artifact_run_id: int | None = None) -> dict:
    """Produce selection blockers; intentionally do not rank model candidates."""
    if not 2 <= len(paths) <= 5:
        raise ValueError("expected 2..5 score/profile pairs")
    if artifact_run_id is not None and (
        type(artifact_run_id) is not int or artifact_run_id <= 0
    ):
        raise ValueError("invalid workflow run")
    inputs = [_load_pair(score, profile) for score, profile in paths]
    first = inputs[0]["score"]
    for item in inputs[1:]:
        score = item["score"]
        if (score["suite_sha256"] != first["suite_sha256"]
                or item["case_ids"] != inputs[0]["case_ids"]
                or score["sample_claims"] != first["sample_claims"]
                or score["prompt_version"] != first["prompt_version"]):
            raise ValueError("benchmark inputs are not paired")
    if len({item["score"]["backend"] for item in inputs}) != len(inputs):
        raise ValueError("duplicate backend identity")
    backends = []
    for entry in inputs:
        score, profile = entry["score"], entry["profile"]
        count = score["sample_claims"]
        run_errors = [run["error_claims"] for run in profile["runs"]]
        error_fractions = [_ratio(errors, count) for errors in run_errors]
        output_status = (
            "NO_VALID_MODEL_OUTPUT" if all(errors == count for errors in run_errors)
            else "PARTIAL_MODEL_ERRORS" if any(run_errors)
            else "NO_MODEL_ERRORS_OBSERVED"
        )
        dimensions = {}
        for dimension in LABEL_FIELDS:
            m = score["by_dimension"][dimension]
            metrics = {
                **m, "coverage": _ratio(m["supported"], m["eligible"]),
                "conditional_draft_agreement": _ratio(m["correct"], m["supported"]),
                "draft_agreement_on_eligible": _ratio(m["correct"], m["eligible"]),
            }
            dimensions[dimension] = metrics
        blockers = ["UNREVIEWED_AUTHOR_DRAFTS", "ORACLE_CLAIM_SPANS",
                    "NO_INDEPENDENT_HOLDOUT", "END_TO_END_MEMORY_SAFETY_UNTESTED",
                    "TARGET_HARDWARE_LATENCY_UNVERIFIED"]
        if output_status == "NO_VALID_MODEL_OUTPUT":
            blockers.append("ALL_CLASSIFICATIONS_FAILED")
        elif output_status == "PARTIAL_MODEL_ERRORS":
            blockers.append("PARTIAL_CLASSIFICATION_ERRORS")
        if any(
            metrics["eligible"] < MIN_REVIEWED_ELIGIBLE_PROPOSAL
            for metrics in dimensions.values()
        ):
            blockers.append("INSUFFICIENT_EVALUATION_SAMPLE")
        if any(
            metrics["eligible"] > 0
            and metrics["coverage"] < MIN_COVERAGE_PROPOSAL
            for metrics in dimensions.values()
        ):
            blockers.append("INSUFFICIENT_DIMENSION_COVERAGE")
        if any(
            metrics["supported"] > 0
            and metrics["conditional_draft_agreement"] < MIN_CONDITIONAL_AGREEMENT_PROPOSAL
            for metrics in dimensions.values()
        ):
            blockers.append("LOW_PROVISIONAL_DIMENSION_AGREEMENT")
        if any(value > MAX_ERROR_FRACTION_PROPOSAL for value in error_fractions):
            blockers.append("MODEL_OUTPUT_RELIABILITY_BELOW_PROPOSAL")
        backends.append({
            "backend": score["backend"], "model": profile.get("model"),
            "score_sha256": entry["score_sha256"],
            "profile_sha256": entry["profile_sha256"],
            "claims_per_run": count, "repetitions": len(run_errors),
            "errors_per_run": run_errors,
            "error_fractions_per_run": error_fractions,
            "warmup_errors": profile.get("warmup", {}).get("errors"),
            "output_status": output_status,
            "dimensions": dimensions,
            "median_suite_ms": profile.get("timing", {}).get("median_suite_ms"),
            "median_claim_inference_ms": score.get("median_claim_inference_ms"),
            "selection_status": "NOT_ELIGIBLE_FOR_PRODUCTION_SELECTION",
            "blockers": blockers,
        })
    return {
        "version": VERSION, "source_run_id": artifact_run_id,
        "source_suite_sha256": first["suite_sha256"],
        "prompt_version": first["prompt_version"],
        "annotation_status": first["annotation_status"],
        "reference_mode": first["reference_mode"],
        "sample_cases": first["sample_cases"],
        "sample_claims": first["sample_claims"],
        "backends": backends,
        "proposed_selection_gates": {
            "status": "PROPOSAL_NOT_APPROVED_FOR_ROUTING",
            "minimum_independently_adjudicated_eligible_per_dimension":
                MIN_REVIEWED_ELIGIBLE_PROPOSAL,
            "max_model_error_fraction_per_run": MAX_ERROR_FRACTION_PROPOSAL,
            "minimum_coverage_per_required_dimension": MIN_COVERAGE_PROPOSAL,
            "minimum_conditional_agreement_per_required_dimension":
                MIN_CONDITIONAL_AGREEMENT_PROPOSAL,
            "must_measure_target_hardware_p95": True,
            "must_test_memory_admission_and_correction_safety": True,
            "must_test_end_to_end_extraction_and_qualifiers": True,
            "must_show_paired_value_over_deterministic_baseline": True,
            "must_obtain_explicit_routing_authorization": True,
        },
        "decision": "NO_MODEL_SELECTED",
        "notes": [
            "Any error-only run has no semantic classification evidence; a green workflow is insufficient.",
            "Coverage and conditional agreement are against synthetic author drafts, not independent gold.",
            "spaCy baseline is lexical-only; its tiny supported subset cannot establish general competence.",
            "No model rankings, confidence intervals or deployment claims follow from these 15 cases.",
            "Aggregate reports cannot identify which individual claims had a semantic disagreement.",
            "No state or Memory writes and no runtime route changes.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", nargs=2, action="append", metavar=("SCORES", "PROFILE"),
                        required=True, type=Path)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite report")
    try:
        report = analyze_pairs(args.pair, artifact_run_id=args.run_id)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        parser.error("invalid, unpaired, or unavailable benchmark evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
