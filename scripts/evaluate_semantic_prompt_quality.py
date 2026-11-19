#!/usr/bin/env python3
"""MVP-02C.14I: compare two versioned semantic prompts using real local Qwen.

Offline reference-span-conditioned experiment; provisional author-draft labels
are not independent gold and no prompt can be promoted by this experiment.
Only text-free dimension aggregates, immutable fingerprints, and bounded
failure categories are written, never source text or raw model completions.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable

from benchmark_semantic_models import _model_provenance
from diagnose_semantic_model_failures import _run_mode
from evaluate_semantic_models import (
    LABEL_FIELDS, OLLAMA_PROMPT_VARIANTS, OLLAMA_PROMPT_VERSIONS,
    OllamaJsonProbe, _ollama_instruction,
)

VERSION = "mvp02c14i.semantic_prompt_quality_experiment.v1"
VARIANTS = ("baseline", "semantic_scope_v1")
assert VARIANTS == OLLAMA_PROMPT_VARIANTS


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _validate_run(result: dict) -> None:
    claims = result["sample_claims"]
    valid = result["valid_prediction_claims"]
    errors = result["error_claims"]
    if (type(claims) is not int or claims <= 0
            or type(valid) is not int or valid < 0
            or type(errors) is not int or errors < 0
            or valid + errors != claims):
        raise ValueError("invalid model claim/error totals")
    if not isinstance(result["failure_histogram"], dict) or any(
        type(n) is not int or n <= 0 for n in result["failure_histogram"].values()
    ) or sum(result["failure_histogram"].values()) != errors:
        raise ValueError("invalid model failure histogram")
    if set(result["by_dimension"]) != set(LABEL_FIELDS):
        raise ValueError("incomplete semantic dimension scores")
    for field in LABEL_FIELDS:
        metrics = result["by_dimension"][field]
        keys = ("eligible", "supported", "correct", "incorrect", "abstained",
                "unscored_reference")
        if any(type(metrics.get(key)) is not int or metrics[key] < 0 for key in keys):
            raise ValueError("invalid dimension count")
        if (metrics["eligible"] + metrics["unscored_reference"] != claims
                or metrics["supported"] + metrics["abstained"] != metrics["eligible"]
                or metrics["correct"] + metrics["incorrect"] != metrics["supported"]
                or metrics["supported"] > valid):
            raise ValueError("inconsistent dimension scores")


def run_experiment(
    *, model: str, endpoint: str, timeout_s: float, suite: Path,
    case_ids: tuple[str, ...] | None = None, repetitions: int = 2,
    backend_factory: Callable = OllamaJsonProbe,
    provenance_resolver: Callable = _model_provenance,
) -> dict:
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise ValueError("repetitions must be 1..3")
    rows = {variant: [] for variant in VARIANTS}
    provenance = None
    reference_denominators = None
    pairing = None

    # Interleave variants to reduce simple thermal/order confounds.
    for repeat in range(repetitions):
        for variant in VARIANTS:
            def factory(**kwargs):
                return backend_factory(prompt_variant=variant, **kwargs)

            result, backend = _run_mode(
                model=model, endpoint=endpoint, timeout_s=timeout_s,
                suite=suite, case_ids=case_ids, mode="schema",
                factory=factory,
            )
            _validate_run(result)
            if result.get("mode") != "schema" or result.get("format_schema_sha256") is None:
                raise ValueError("prompt experiment requires identical schema mode")
            fingerprint = provenance_resolver(
                backend_name="ollama", backend=backend,
                model_dir=None, model_name=model,
            )
            if (not isinstance(fingerprint, dict)
                    or fingerprint.get("digest_verified") is not True
                    or not isinstance(fingerprint.get("digest"), str)):
                raise ValueError("unverified model digest")
            if provenance is None:
                provenance = fingerprint
            elif fingerprint != provenance:
                raise ValueError("model provenance changed between prompt runs")

            current_pairing = (
                result["suite_sha256"], tuple(result["case_ids"]),
                result["sample_cases"], result["sample_claims"],
                result["format_schema_sha256"],
            )
            if pairing is None:
                pairing = current_pairing
            elif pairing != current_pairing:
                raise ValueError("unpaired prompt experiment cases or schema")
            denominators = tuple(
                (field, result["by_dimension"][field]["eligible"],
                 result["by_dimension"][field]["unscored_reference"])
                for field in LABEL_FIELDS
            )
            if reference_denominators is None:
                reference_denominators = denominators
            elif denominators != reference_denominators:
                raise ValueError("reference eligibility changed across prompts")
            rows[variant].append(result)

    compared = []
    for variant in VARIANTS:
        runs = rows[variant]
        compared.append({
            "variant": variant,
            "prompt_version": OLLAMA_PROMPT_VERSIONS[variant],
            "prompt_sha256": sha256(_ollama_instruction(variant).encode()).hexdigest(),
            "repetitions": [{
                "index": i + 1,
                "warmup_code": result["warmup_code"],
                "sample_claims": result["sample_claims"],
                "valid_prediction_claims": result["valid_prediction_claims"],
                "error_claims": result["error_claims"],
                "failure_histogram": result["failure_histogram"],
                "median_claim_inference_ms": result["median_claim_inference_ms"],
                "total_inference_ms": result["total_inference_ms"],
                "by_dimension": {
                    key: {
                        **result["by_dimension"][key],
                        "coverage": _rate(
                            result["by_dimension"][key]["supported"],
                            result["by_dimension"][key]["eligible"],
                        ),
                        "conditional_draft_agreement": _rate(
                            result["by_dimension"][key]["correct"],
                            result["by_dimension"][key]["supported"],
                        ),
                        "draft_agreement_on_eligible": _rate(
                            result["by_dimension"][key]["correct"],
                            result["by_dimension"][key]["eligible"],
                        ),
                    } for key in LABEL_FIELDS
                },
            } for i, result in enumerate(runs)],
        })

    # Preserve first-to-first, second-to-second *aggregate* changes. They
    # are not a statistical significance test or paired per-claim analysis.
    deltas = []
    for index in range(repetitions):
        old = rows["baseline"][index]
        new = rows["semantic_scope_v1"][index]
        deltas.append({
            "index": index + 1,
            "valid_prediction_difference":
                new["valid_prediction_claims"] - old["valid_prediction_claims"],
            "by_dimension": {
                field: {
                    "supported_difference": (
                        new["by_dimension"][field]["supported"]
                        - old["by_dimension"][field]["supported"]
                    ),
                    "correct_draft_difference": (
                        new["by_dimension"][field]["correct"]
                        - old["by_dimension"][field]["correct"]
                    ),
                } for field in LABEL_FIELDS
            },
        })
    return {
        "version": VERSION,
        "reference_mode": "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY",
        "annotation_status": "AUTHOR_DRAFT_NOT_INDEPENDENT",
        "model": provenance,
        "source_suite_sha256": pairing[0],
        "case_ids": list(pairing[1]),
        "sample_cases": pairing[2],
        "sample_claims": pairing[3],
        "schema_sha256": pairing[4],
        "format_mode": "schema",
        "generation": {"temperature": 0, "num_predict": 350},
        "experiment_order": "INTERLEAVED_BASELINE_CANDIDATE",
        "candidates": compared,
        "aggregate_differences_by_repetition": deltas,
        "decision": "NO_PROMPT_APPROVED_FOR_PRODUCTION",
        "limitations": [
            "Reference claim quotes are oracle-provided, not extracted by the model.",
            "Author-written draft labels are not independently reviewed ground truth.",
            "Aggregate count differences are not claim-paired correctness or statistical proof.",
            "Prompt candidate was proposed after inspecting prior draft-label aggregates.",
            "No production model, prompt routing, Memory, or persona state changes.",
        ],
    }


def main(argv=None, *, backend_factory=OllamaJsonProbe,
         provenance_resolver=_model_provenance) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c14d_semantic_drafts.json"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-s", type=float, default=90)
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite evidence")
    try:
        output = run_experiment(
            model=args.model, endpoint=args.ollama_url,
            timeout_s=args.timeout_s, suite=args.suite,
            case_ids=tuple(args.case_ids) if args.case_ids else None,
            repetitions=args.repetitions,
            backend_factory=backend_factory,
            provenance_resolver=provenance_resolver,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(output, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, TypeError, KeyError):
        parser.error("invalid, unpaired or unavailable local prompt evaluation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
