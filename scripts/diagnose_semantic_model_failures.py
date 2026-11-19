#!/usr/bin/env python3
"""MVP-02C.14H: diagnose local Ollama classification failures without text logs.

Runs the same v14E span-conditioned synthetic suite in current JSON mode and
opt-in JSON-schema mode. Records ONLY fixed, source-independent failure codes,
dimension aggregates and immutable model provenance. Not a model selection,
memory write, full extraction test, or a production routing change.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import Callable

from benchmark_semantic_models import _model_provenance
from evaluate_semantic_models import (
    LABEL_FIELDS, OLLAMA_LABEL_SCHEMA_V1, OllamaJsonProbe, PROMPT_VERSION,
    evaluate_probe,
)

VERSION = "mvp02c14h.ollama_failure_diagnostics.v1"
SCHEMA_VERSION = "mvp02c14h.ollama_labels_json_schema.v1"
KNOWN_CODES = frozenset({
    "VALID_PREDICTION", "HTTP_NON_200", "HTTP_4XX", "HTTP_5XX",
    "HTTP_OTHER", "TRANSPORT_TIMEOUT", "TRANSPORT_ERROR",
    "ENVELOPE_OVERSIZE", "ENVELOPE_JSON_INVALID", "ENVELOPE_INVALID",
    "CONTENT_INVALID", "CONTENT_EMPTY", "OUTPUT_TRUNCATED",
    "CONTENT_JSON_INVALID", "LABEL_NOT_OBJECT", "LABEL_FIELD_SET_INVALID",
    "LABEL_VALUE_INVALID",
})


def _record_one(code: str, events: list[str]) -> None:
    if code not in KNOWN_CODES:
        # Never archive raw exception strings or arbitrary model content.
        raise ValueError("unknown diagnostic event")
    events.append(code)


def _run_mode(
    *, model: str, endpoint: str, timeout_s: float, suite: Path,
    case_ids: tuple[str, ...] | None, mode: str,
    factory: Callable = OllamaJsonProbe,
) -> tuple[dict, object]:
    events: list[str] = []
    backend = factory(
        model=model, url=endpoint, timeout_s=timeout_s,
        format_mode=mode, diagnostic_sink=lambda code: _record_one(code, events),
    )
    if not isinstance(getattr(backend, "backend_id", None), str):
        raise ValueError("diagnostic backend identity required")
    started = perf_counter()
    try:
        backend.predict(source="The lights are on.", quote="The lights are on")
    except Exception:
        pass  # Expected diagnostic outcome; never log raw exceptions.
    if len(events) != 1:
        raise ValueError("warmup diagnostic event missing or duplicated")
    warmup_code = events.pop()
    measured = evaluate_probe(suite, backend=backend, case_ids=case_ids)
    if len(events) != measured["sample_claims"]:
        raise ValueError("model diagnostic events do not match claim count")
    counts = Counter(events)
    supported = counts["VALID_PREDICTION"]
    if supported != measured["sample_claims"] - measured["error_claims"]:
        raise ValueError("model success/error totals inconsistent with diagnostics")
    if counts.get("VALID_PREDICTION", 0) + sum(
        count for key, count in counts.items() if key != "VALID_PREDICTION"
    ) != measured["sample_claims"]:
        raise ValueError("invalid diagnostic event count")
    for dimension in LABEL_FIELDS:
        if measured["by_dimension"][dimension]["supported"] > supported:
            raise ValueError("supported dimensions exceed valid model predictions")
    return {
        "mode": mode,
        "format_schema_version": SCHEMA_VERSION if mode == "schema" else None,
        "format_schema_sha256": (
            sha256(json.dumps(OLLAMA_LABEL_SCHEMA_V1, sort_keys=True,
                              separators=(",", ":")).encode()).hexdigest()
            if mode == "schema" else None
        ),
        "backend": backend.backend_id,
        "warmup_code": warmup_code,
        "sample_cases": measured["sample_cases"],
        "sample_claims": measured["sample_claims"],
        "valid_prediction_claims": supported,
        "error_claims": measured["error_claims"],
        "status": ("NO_VALID_PREDICTIONS" if supported == 0
                   else "PARTIAL_PREDICTION_FAILURES" if measured["error_claims"]
                   else "NO_PREDICTION_FAILURES_OBSERVED"),
        "failure_histogram": dict(sorted(
            (name, count) for name, count in counts.items()
            if name != "VALID_PREDICTION"
        )),
        "median_claim_inference_ms": measured["median_claim_inference_ms"],
        "total_inference_ms": measured["total_inference_ms"],
        "observed_total_wall_ms": round((perf_counter() - started) * 1000, 3),
        "by_dimension": {
            field: {key: measured["by_dimension"][field][key]
                    for key in ("eligible", "supported", "correct", "incorrect",
                                "abstained", "unscored_reference")}
            for field in LABEL_FIELDS
        },
        "case_ids": [case["id"] for case in measured["cases"]],
        "suite_sha256": measured["suite_sha256"],
    }, backend


def run_diagnostics(
    *, model: str, endpoint: str, timeout_s: float, suite: Path,
    case_ids: tuple[str, ...] | None = None,
    modes: tuple[str, ...] = ("json", "schema"),
    backend_factory: Callable = OllamaJsonProbe,
    provenance_resolver: Callable = _model_provenance,
) -> dict:
    if not modes or len(set(modes)) != len(modes) or set(modes) - {"json", "schema"}:
        raise ValueError("modes must be distinct json/schema variants")
    runs = []
    identity = None
    for mode in modes:
        result, backend = _run_mode(
            model=model, endpoint=endpoint, timeout_s=timeout_s,
            suite=suite, case_ids=case_ids, mode=mode,
            factory=backend_factory,
        )
        provenance = provenance_resolver(
            backend_name="ollama", backend=backend,
            model_dir=None, model_name=model,
        )
        if not isinstance(provenance, dict) or not provenance.get("digest_verified"):
            raise ValueError("unverifiable local model digest")
        if identity is None:
            identity = provenance
        elif provenance != identity:
            raise ValueError("model digest/provenance changed between modes")
        runs.append(result)
    first = runs[0]
    for result in runs[1:]:
        if (result["sample_claims"] != first["sample_claims"]
                or result["suite_sha256"] != first["suite_sha256"]
                or result["case_ids"] != first["case_ids"]):
            raise ValueError("unpaired diagnostic modes")
        for field in LABEL_FIELDS:
            first_counts = first["by_dimension"][field]
            other_counts = result["by_dimension"][field]
            if (first_counts["eligible"], first_counts["unscored_reference"]) != (
                other_counts["eligible"], other_counts["unscored_reference"]
            ):
                raise ValueError("reference denominators differ across modes")
    return {
        "version": VERSION,
        "prompt_version": PROMPT_VERSION,
        "reference_mode": "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY",
        "annotation_status": "AUTHOR_DRAFT_NOT_INDEPENDENT",
        "source_suite_sha256": first["suite_sha256"],
        "model": identity,
        "sample_cases": first["sample_cases"],
        "sample_claims": first["sample_claims"],
        "case_ids": first["case_ids"],
        "modes": runs,
        "decision": "NO_PRODUCTION_MODEL_SELECTED",
        "caveats": [
            "Failure categories are control-flow stages, never original exception messages.",
            "No raw user text, prompts, responses, labels or source snippets are archived.",
            "Valid JSON is not proof of semantic correctness.",
            "Author-draft labels and reference-provided claim spans are not independent gold.",
            "No runtime semantic routing, training or Memory admission changes.",
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
    parser.add_argument("--modes", nargs="+", choices=("json", "schema"),
                        default=["json", "schema"])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite an existing diagnostics report")
    try:
        data = run_diagnostics(
            model=args.model, endpoint=args.ollama_url,
            timeout_s=args.timeout_s, suite=args.suite,
            case_ids=tuple(args.case_ids) if args.case_ids else None,
            modes=tuple(args.modes), backend_factory=backend_factory,
            provenance_resolver=provenance_resolver,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError, TypeError, KeyError):
        parser.error("diagnostic run failed validation or local model unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
