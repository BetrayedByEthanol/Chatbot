#!/usr/bin/env python3
"""MVP-02C.14J: local-only comparison against adjudicated human claim labels.

Will NOT score unlabeled/draft packets. No model sees labels; no raw source
sent to reports; model outputs remain advisory and never authorize Memory.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from benchmark_semantic_models import _decision_thresholds, _host, _model_provenance
from evaluate_semantic_models import (
    DebertaNliProbe, LABEL_FIELDS, OllamaJsonProbe, ProbeLabels,
    SpacyStructuralProbe, _add_score, _blank_counts, _unknown,
)
from review_independent_semantic_labels import (
    GOLD_STATUS, GOLD_VERSION, load_packet,
)

VERSION = "mvp02c14j.reviewed_model_comparison.v1"
PAIR_VERSION = "mvp02c14j.paired_reviewed_comparison.v1"


def _load(path: Path) -> tuple[dict, str]:
    blob = path.read_bytes()
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data, sha256(blob).hexdigest()


def _write_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")


def load_reviewed_reference(*, gold_path: Path, packet_path: Path):
    gold, gold_sha = _load(gold_path)
    packet, packet_sha, keys = load_packet(packet_path)
    if (gold.get("version") != GOLD_VERSION
            or gold.get("annotation_status") != GOLD_STATUS
            or gold.get("review_process")
                != "TWO_BLINDED_REVIEWERS_AND_DISTINCT_HUMAN_ADJUDICATOR"
            or gold.get("independence")
                != "HUMAN_ATTESTED_NOT_TECHNICALLY_VERIFIABLE"
            or gold.get("source_packet_sha256") != packet_sha
            or gold.get("claim_count") != len(keys)):
        raise ValueError("unreviewed/mismatched semantic reference")
    prov = gold.get("review_provenance")
    if (not isinstance(prov, dict)
            or set(prov) != {
                "reviewer_a_id", "reviewer_a_sha256", "reviewer_b_id",
                "reviewer_b_sha256", "adjudicator_id", "adjudicator_sha256",
            } or len({prov["reviewer_a_id"], prov["reviewer_b_id"],
                      prov["adjudicator_id"]}) != 3
            or any(not isinstance(v, str) or not v for v in prov.values())):
        raise ValueError("missing distinct human adjudication provenance")
    items = gold.get("cases")
    if not isinstance(items, list) or len(items) != len(packet["cases"]):
        raise ValueError("reviewed case set differs from blind packet")
    for actual, expected in zip(items, packet["cases"]):
        if (not isinstance(actual, dict)
                or set(actual) != {"id", "text", "claims"}
                or (actual["id"], actual["text"]) != (expected["id"], expected["text"])
                or not isinstance(actual["claims"], list)
                or len(actual["claims"]) != len(expected["claims"])):
            raise ValueError("reviewed source text/case mismatch")
        for actual_span, original in zip(actual["claims"], expected["claims"]):
            if (not isinstance(actual_span, dict)
                    or set(actual_span) != {"start", "end", "text", "labels"}
                    or any(actual_span[key] != original[key]
                           for key in ("start", "end", "text"))):
                raise ValueError("reviewed claim quote/span mismatch")
            # Strict enum/unknown validation with no automatic coercion.
            ProbeLabels.from_mapping(actual_span["labels"])
    return gold, gold_sha, packet_sha


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _run_one(*, cases: list, backend) -> dict:
    counts = {field: _blank_counts() for field in LABEL_FIELDS}
    errors = 0
    durations = []
    for case in cases:
        for claim in case["claims"]:
            start = perf_counter()
            try:
                labels = backend.predict(source=case["text"], quote=claim["text"])
                if not isinstance(labels, ProbeLabels):
                    raise ValueError("backend must return validated prediction")
                labels = ProbeLabels.from_mapping(labels.model_dump(mode="json"))
            except Exception:
                # Never include raw exception text in benchmark outputs.
                errors += 1
                labels = ProbeLabels()
            durations.append((perf_counter() - start) * 1000)
            for field in LABEL_FIELDS:
                _add_score(
                    counts[field], field, claim["labels"][field],
                    getattr(labels, field)
                )
    claims = len(durations)
    for field, score in counts.items():
        if (score["eligible"] + score["unscored_reference"] != claims
                or score["supported"] + score["abstained"] != score["eligible"]
                or score["correct"] + score["incorrect"] != score["supported"]
                or score["supported"] > claims - errors):
            raise ValueError(f"invalid reviewed score arithmetic: {field}")
    return {
        "claims": claims, "errors": errors,
        "median_claim_inference_ms": round(median(durations), 3),
        "by_dimension": {
            field: {
                **value,
                "coverage": _ratio(value["supported"], value["eligible"]),
                "conditional_reviewed_agreement":
                    _ratio(value["correct"], value["supported"]),
                "reviewed_agreement_on_eligible":
                    _ratio(value["correct"], value["eligible"]),
            } for field, value in counts.items()
        },
    }


def benchmark_reviewed(*, gold_path: Path, packet_path: Path, backend,
                       backend_name: str, model_dir: Path | None = None,
                       model_name: str | None = None,
                       repetitions: int = 2,
                       provenance_resolver=_model_provenance) -> dict:
    if type(repetitions) is not int or not 1 <= repetitions <= 4:
        raise ValueError("repetitions must be 1..4")
    if backend_name not in ("spacy", "deberta", "ollama"):
        raise ValueError("unsupported reviewed model backend")
    gold, gold_sha, packet_sha = load_reviewed_reference(
        gold_path=gold_path, packet_path=packet_path
    )
    if not isinstance(getattr(backend, "backend_id", None), str):
        raise ValueError("model backend identity required")
    fingerprint = provenance_resolver(
        backend_name=backend_name, backend=backend,
        model_dir=model_dir, model_name=model_name,
    )
    measurements = [_run_one(cases=gold["cases"], backend=backend)
                    for _ in range(repetitions)]
    expected_denoms = None
    for record in measurements:
        denoms = tuple(
            (field, record["by_dimension"][field]["eligible"],
             record["by_dimension"][field]["unscored_reference"])
            for field in LABEL_FIELDS
        )
        if expected_denoms is None:
            expected_denoms = denoms
        elif denoms != expected_denoms:
            raise ValueError("reference denominators changed across model runs")
    return {
        "version": VERSION,
        "reference_status": GOLD_STATUS,
        "human_independence": gold["independence"],
        "reference_mode": "OFFLINE_ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY",
        "reviewed_reference_sha256": gold_sha,
        "source_packet_sha256": packet_sha,
        "case_ids": [row["id"] for row in gold["cases"]],
        "sample_cases": len(gold["cases"]),
        "sample_claims": gold["claim_count"],
        "backend": backend.backend_id,
        "model": fingerprint,
        "nli_decision_thresholds": _decision_thresholds(
            backend_name=backend_name, backend=backend,
        ),
        "host": _host(),
        "runs": measurements,
        "selection_decision": "NO_MODEL_SELECTED",
        "limits": [
            "Reviewer independence is attested but cannot be technically verified.",
            "Evaluation uses oracle claim spans and does not test extraction.",
            "Synthetic pilot is insufficient for deployment model promotion.",
            "Local-only read-only models, no Memory/Goal/Conversation writes.",
        ],
    }


def compare_reports(paths: list[Path], *, gold_path: Path,
                    packet_path: Path) -> dict:
    if not 2 <= len(paths) <= 5:
        raise ValueError("need 2..5 model reports")
    gold, actual_gold_sha, actual_packet_sha = load_reviewed_reference(
        gold_path=gold_path, packet_path=packet_path
    )
    expected_ids = [case["id"] for case in gold["cases"]]
    expected_claims = gold["claim_count"]
    # Anchor the reference denominators to the actual human fixture;
    # mere cross-backend equality could conceal uniformly corrupted labels.
    expected_denominators = tuple(
        (field,
         sum(not _unknown(field, claim["labels"][field])
             for case in gold["cases"] for claim in case["claims"]),
         sum(_unknown(field, claim["labels"][field])
             for case in gold["cases"] for claim in case["claims"]))
        for field in LABEL_FIELDS
    )
    loaded = [_load(path) for path in paths]
    first = loaded[0][0]
    same = ("reviewed_reference_sha256", "source_packet_sha256", "case_ids",
            "sample_cases", "sample_claims", "reference_status", "reference_mode")
    seen = set()
    baseline_denominators = None
    summaries = []
    for report, digest in loaded:
        if (report.get("version") != VERSION
                or report.get("reference_status") != GOLD_STATUS
                or report.get("human_independence")
                    != "HUMAN_ATTESTED_NOT_TECHNICALLY_VERIFIABLE"
                or report.get("reviewed_reference_sha256") != actual_gold_sha
                or report.get("source_packet_sha256") != actual_packet_sha
                or report.get("case_ids") != expected_ids
                or report.get("sample_cases") != len(expected_ids)
                or report.get("sample_claims") != expected_claims
                or any(report.get(key) != first.get(key) for key in same)):
            raise ValueError("unpaired or unreviewed model reports")
        identity = report.get("backend")
        if not isinstance(identity, str) or identity in seen:
            raise ValueError("duplicate or invalid candidate backend")
        seen.add(identity)
        if not isinstance(report.get("runs"), list) or not report["runs"]:
            raise ValueError("missing model runs")
        runs = []
        for run in report["runs"]:
            if (type(run.get("claims")) is not int
                    or run["claims"] != first["sample_claims"]
                    or type(run.get("errors")) is not int
                    or not 0 <= run["errors"] <= run["claims"]
                    or not isinstance(run.get("by_dimension"), dict)
                    or set(run["by_dimension"]) != set(LABEL_FIELDS)):
                raise ValueError("invalid model run counts")
            denoms = []
            for field in LABEL_FIELDS:
                score = run["by_dimension"][field]
                required = ("eligible", "unscored_reference", "supported",
                            "abstained", "correct", "incorrect")
                if any(type(score.get(key)) is not int or score[key] < 0
                       for key in required):
                    raise ValueError("invalid dimension score")
                if (score["eligible"] + score["unscored_reference"] != run["claims"]
                        or score["supported"] + score["abstained"] != score["eligible"]
                        or score["correct"] + score["incorrect"] != score["supported"]
                        or score["supported"] > run["claims"] - run["errors"]):
                    raise ValueError("corrupted model scoring arithmetic")
                denoms.append((field, score["eligible"], score["unscored_reference"]))
            if tuple(denoms) != expected_denominators:
                raise ValueError("reference denominators disagree with reviewed fixture")
            if baseline_denominators is None:
                baseline_denominators = denoms
            elif denoms != baseline_denominators:
                raise ValueError("reference denominators change across candidates")
            runs.append({
                "errors": run["errors"],
                "by_dimension": {
                    field: {
                        "eligible": run["by_dimension"][field]["eligible"],
                        "supported": run["by_dimension"][field]["supported"],
                        "correct": run["by_dimension"][field]["correct"],
                        "abstained": run["by_dimension"][field]["abstained"],
                    } for field in LABEL_FIELDS
                }
            })
        summaries.append({
            "backend": identity, "report_sha256": digest, "model": report.get("model"),
            "runs": runs,
        })
    return {
        "version": PAIR_VERSION,
        "reviewed_reference_sha256": first["reviewed_reference_sha256"],
        "source_packet_sha256": first["source_packet_sha256"],
        "sample_cases": first["sample_cases"],
        "sample_claims": first["sample_claims"],
        "candidates": summaries,
        "decision": "NO_PRODUCTION_MODEL_SELECTED",
        "limitations": [
            "This is an attested pilot, not a validated generalization estimate.",
            "Comparison across candidates is diagnostic; not automatically ranked.",
            "Abstentions must be evaluated independently of conditional agreement.",
            "Claim extraction, qualifiers, memory safety and p95 target latency untested.",
        ],
    }


def _instantiate(args):
    if args.backend == "spacy":
        return SpacyStructuralProbe()
    if args.backend == "deberta":
        if args.model_dir is None:
            raise ValueError("--model-dir is required for local DeBERTa")
        return DebertaNliProbe(
            args.model_dir, device=args.device,
            min_score=args.min_score, min_margin=args.min_margin,
        )
    if args.model is None:
        raise ValueError("--model is required for local Ollama")
    return OllamaJsonProbe(
        model=args.model, url=args.ollama_url,
        timeout_s=args.timeout_s, format_mode="schema",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    run = subs.add_parser("run")
    run.add_argument("--gold", type=Path, required=True)
    run.add_argument("--packet", type=Path, required=True)
    run.add_argument("--backend", choices=("spacy", "deberta", "ollama"), required=True)
    run.add_argument("--model-dir", type=Path)
    run.add_argument("--model")
    run.add_argument("--device", default="cpu")
    run.add_argument("--min-score", type=float, default=.65)
    run.add_argument("--min-margin", type=float, default=.10)
    run.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    run.add_argument("--timeout-s", type=float, default=90)
    run.add_argument("--repetitions", type=int, default=2)
    run.add_argument("--report", type=Path, required=True)
    compare = subs.add_parser("compare")
    compare.add_argument("--reports", nargs="+", type=Path, required=True)
    compare.add_argument("--gold", type=Path, required=True)
    compare.add_argument("--packet", type=Path, required=True)
    compare.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite benchmark evidence")
    try:
        if args.action == "run":
            backend = _instantiate(args)
            result = benchmark_reviewed(
                gold_path=args.gold, packet_path=args.packet,
                backend=backend, backend_name=args.backend,
                model_dir=args.model_dir, model_name=args.model,
                repetitions=args.repetitions,
            )
        else:
            result = compare_reports(args.reports, gold_path=args.gold,
                                     packet_path=args.packet)
        _write_once(args.report, result)
    except (OSError, ValueError, TypeError, KeyError):
        parser.error("missing validated human reference, local model or paired reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
