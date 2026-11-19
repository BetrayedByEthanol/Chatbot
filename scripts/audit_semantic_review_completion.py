#!/usr/bin/env python3
"""MVP-02C.14K: review-completion audit and guarded local model comparison.

This script does NOT manufacture human annotations. It seals/checks submitted
human forms against the exact adjudicated fixture before running real models.
The status command is safe to run with NO human submissions available.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from compare_independent_semantic_models import (
    _instantiate, benchmark_reviewed, compare_reports, load_reviewed_reference,
)
from evaluate_semantic_models import LABEL_FIELDS, _unknown
from review_independent_semantic_labels import (
    _load, _validate_form, _write_once, adjudicate, load_packet, make_form,
)

VERSION = "mvp02c14k.human_review_completion_audit.v1"
RUN_VERSION = "mvp02c14k.review_gated_model_report.v1"
COMPARISON_VERSION = "mvp02c14k.review_gated_model_comparison.v1"
MIN_ELIGIBLE_PER_REQUIRED_DIMENSION_PROPOSAL = 200
REPORT_STATUS_PENDING = "PENDING_GENUINE_HUMAN_ANNOTATIONS"
REPORT_STATUS_REVIEWED = "HUMAN_REVIEW_ATTESTED_PILOT_ONLY"
SEAL_STATUS = "HUMAN_ATTESTATION_NOT_INDEPENDENTLY_VERIFIABLE"


def _hash_document(value: dict) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def prepare_forms(*, packet_path: Path, output_dir: Path,
                  reviewer_a_id: str, reviewer_b_id: str,
                  adjudicator_id: str) -> tuple[Path, Path, Path]:
    """Generate private blank forms, never fill human labels or attestations."""
    identifiers = (reviewer_a_id, reviewer_b_id, adjudicator_id)
    if len(set(identifiers)) != 3:
        raise ValueError("three distinct assigned human actor aliases required")
    # Validate EVERYTHING before creating the directory, avoiding half-complete
    # review packages on invalid input. The directory must be new.
    forms = (
        make_form(packet_path=packet_path, actor_id=reviewer_a_id,
                  role="reviewer"),
        make_form(packet_path=packet_path, actor_id=reviewer_b_id,
                  role="reviewer"),
        make_form(packet_path=packet_path, actor_id=adjudicator_id,
                  role="adjudicator"),
    )
    if output_dir.exists():
        raise ValueError("private review directory already exists")
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    paths = tuple(output_dir / name for name in (
        "reviewer-a.json", "reviewer-b.json", "adjudicator.json",
    ))
    for path, form in zip(paths, forms):
        _write_once(path, form)
        path.chmod(0o600)
    return paths


def audit_completed_review(*, packet_path: Path, gold_path: Path,
                           reviewer_a: Path, reviewer_b: Path,
                           adjudicator_form: Path) -> dict:
    """Validate every submitted claim, attestation, digest and final decision."""
    packet, packet_sha, keys = load_packet(packet_path)
    submitted_a, sha_a = _load(reviewer_a)
    submitted_b, sha_b = _load(reviewer_b)
    submitted_c, sha_c = _load(adjudicator_form)
    id_a, a = _validate_form(
        submitted_a, role="reviewer", digest=packet_sha, keys=keys
    )
    id_b, b = _validate_form(
        submitted_b, role="reviewer", digest=packet_sha, keys=keys
    )
    id_c, adjudicator_items = _validate_form(
        submitted_c, role="adjudicator", digest=packet_sha, keys=keys
    )
    if len({id_a, id_b, id_c}) != 3:
        raise ValueError("human actor identities are not distinct")

    # Rebuild independently from the actual signed-off local forms; do not
    # trust a fixture merely because its metadata claims human provenance.
    reconstructed = adjudicate(
        packet_path=packet_path, reviewer_a=reviewer_a, reviewer_b=reviewer_b,
        adjudicator_form=adjudicator_form,
    )
    stored, gold_sha, _ = load_reviewed_reference(
        gold_path=gold_path, packet_path=packet_path
    )
    if stored != reconstructed:
        raise ValueError("adjudicated reference does not match original human forms")
    if any(
        stored["review_provenance"][key] != expected
        for key, expected in (
            ("reviewer_a_sha256", sha_a), ("reviewer_b_sha256", sha_b),
            ("adjudicator_sha256", sha_c),
        )
    ):
        raise ValueError("reviewer evidence checksums changed")

    metrics = {}
    for field in LABEL_FIELDS:
        agreements, known_both, known_agreements = 0, 0, 0
        known_a, known_b, known_gold = 0, 0, 0
        for key in keys:
            av, bv, cv = (a[key][0][field], b[key][0][field],
                          adjudicator_items[key][0][field])
            a_resolved, b_resolved = (not _unknown(field, av),
                                      not _unknown(field, bv))
            known_a += int(a_resolved)
            known_b += int(b_resolved)
            known_gold += int(not _unknown(field, cv))
            agreements += int(av == bv)
            if a_resolved and b_resolved:
                known_both += 1
                known_agreements += int(av == bv)
        disagreements = len(keys) - agreements
        if stored["disagreements_by_dimension"][field] != disagreements:
            raise ValueError("stored reviewer disagreement totals do not match forms")
        metrics[field] = {
            "claims": len(keys),
            "reviewer_a_resolved": known_a,
            "reviewer_b_resolved": known_b,
            "joint_resolved": known_both,
            "adjudicated_resolved": known_gold,
            "adjudicated_unknown": len(keys) - known_gold,
            "raw_agreement_count": agreements,
            "raw_disagreement_count": disagreements,
            "joint_resolved_agreement_count": known_agreements,
            "raw_agreement_fraction": round(agreements / len(keys), 6),
            "joint_resolved_agreement_fraction": (
                round(known_agreements / known_both, 6)
                if known_both else None
            ),
        }

    missing = [
        field for field, entry in metrics.items()
        if entry["adjudicated_resolved"] < MIN_ELIGIBLE_PER_REQUIRED_DIMENSION_PROPOSAL
    ]
    return {
        "version": VERSION, "status": REPORT_STATUS_REVIEWED,
        "human_independence": SEAL_STATUS,
        "source_packet_sha256": packet_sha,
        "adjudicated_reference_sha256": gold_sha,
        "review_evidence": {
            "reviewer_a_sha256": sha_a,
            "reviewer_b_sha256": sha_b,
            "adjudicator_sha256": sha_c,
        },
        "sample_cases": len(packet["cases"]),
        "sample_claims": len(keys),
        "per_dimension": metrics,
        "proposed_min_eligible_per_dimension": (
            MIN_ELIGIBLE_PER_REQUIRED_DIMENSION_PROPOSAL
        ),
        "below_proposed_sample_floor": missing,
        "model_comparison_status": "LOCAL_PILOT_ALLOWED_AFTER_REVIEW",
        "production_selection_status": "NOT_ELIGIBLE_FOR_PRODUCTION_SELECTION",
        "limitations": [
            "Human independence and real-world identities are attestations.",
            "Claims are author-suggested; this does not test extraction.",
            "This small synthetic pilot cannot estimate deployment accuracy.",
            "No raw messages, human label values or reviewer names in this report.",
        ],
    }


def review_status(*, packet_path: Path) -> dict:
    packet, digest, keys = load_packet(packet_path)
    return {
        "version": VERSION,
        "status": REPORT_STATUS_PENDING,
        "source_packet_sha256": digest,
        "sample_cases": len(packet["cases"]),
        "sample_claims": len(keys),
        "reviewers_required": 2,
        "distinct_adjudicators_required": 1,
        "reviewed_gold_available": False,
        "production_selection_status": "BLOCKED_UNREVIEWED",
        "next_action": "OBTAIN_TWO_HUMAN_REVIEWS_AND_DISTINCT_ADJUDICATION",
    }


def run_reviewed(*, packet_path: Path, gold_path: Path,
                 reviewer_a: Path, reviewer_b: Path, adjudicator_form: Path,
                 backend, backend_name: str, model_dir: Path | None = None,
                 model_name: str | None = None, repetitions: int = 2,
                 provenance_resolver=None) -> dict:
    # Gate before any backend.predict or model execution; caller should
    # instantiate optional heavyweight models ONLY after successful audit.
    audit = audit_completed_review(
        packet_path=packet_path, gold_path=gold_path,
        reviewer_a=reviewer_a, reviewer_b=reviewer_b,
        adjudicator_form=adjudicator_form,
    )
    kwargs = {}
    if provenance_resolver is not None:
        kwargs["provenance_resolver"] = provenance_resolver
    result = benchmark_reviewed(
        gold_path=gold_path, packet_path=packet_path, backend=backend,
        backend_name=backend_name, model_dir=model_dir,
        model_name=model_name, repetitions=repetitions, **kwargs,
    )
    return {
        **result,
        "review_gate_version": VERSION,
        "review_audit_sha256": _hash_document(audit),
        "review_gate_status": REPORT_STATUS_REVIEWED,
        "selection_decision": "NO_MODEL_SELECTED",
    }


def compare_reviewed(*, packet_path: Path, gold_path: Path,
                     reviewer_a: Path, reviewer_b: Path,
                     adjudicator_form: Path, reports: list[Path]) -> dict:
    audit = audit_completed_review(
        packet_path=packet_path, gold_path=gold_path,
        reviewer_a=reviewer_a, reviewer_b=reviewer_b,
        adjudicator_form=adjudicator_form,
    )
    digest = _hash_document(audit)
    for path in reports:
        value, _ = _load(path)
        if (value.get("review_gate_version") != VERSION
                or value.get("review_gate_status") != REPORT_STATUS_REVIEWED
                or value.get("review_audit_sha256") != digest):
            raise ValueError("model report was not gated by the same reviewed forms")
    result = compare_reports(
        reports, gold_path=gold_path, packet_path=packet_path
    )
    return {
        **result,
        "version": COMPARISON_VERSION,
        "review_audit_sha256": digest,
        "selection_decision": "NO_PRODUCTION_MODEL_SELECTED",
        "decision": "NO_PRODUCTION_MODEL_SELECTED",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    status = sub.add_parser("status")
    status.add_argument("--packet", type=Path, required=True)
    status.add_argument("--report", type=Path, required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--packet", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--reviewer-a-id", required=True)
    prepare.add_argument("--reviewer-b-id", required=True)
    prepare.add_argument("--adjudicator-id", required=True)

    for name in ("audit", "run", "compare"):
        cli = sub.add_parser(name)
        cli.add_argument("--packet", type=Path, required=True)
        cli.add_argument("--gold", type=Path, required=True)
        cli.add_argument("--reviewer-a", type=Path, required=True)
        cli.add_argument("--reviewer-b", type=Path, required=True)
        cli.add_argument("--adjudicator", type=Path, required=True)
        cli.add_argument("--report", type=Path, required=True)
        if name == "run":
            cli.add_argument(
                "--backend", choices=("spacy", "deberta", "ollama"),
                required=True,
            )
            cli.add_argument("--model-dir", type=Path)
            cli.add_argument("--model")
            cli.add_argument("--device", default="cpu")
            cli.add_argument("--min-score", type=float, default=.65)
            cli.add_argument("--min-margin", type=float, default=.10)
            cli.add_argument("--ollama-url", default="http://127.0.0.1:11434")
            cli.add_argument("--timeout-s", type=float, default=90)
            cli.add_argument("--repetitions", type=int, default=2)
        if name == "compare":
            cli.add_argument("--reports", nargs="+", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action != "prepare" and args.report.exists():
        parser.error("refusing to overwrite existing audit/benchmark evidence")
    try:
        if args.action == "status":
            output = review_status(packet_path=args.packet)
        elif args.action == "prepare":
            prepare_forms(
                packet_path=args.packet, output_dir=args.output_dir,
                reviewer_a_id=args.reviewer_a_id,
                reviewer_b_id=args.reviewer_b_id,
                adjudicator_id=args.adjudicator_id,
            )
            return 0
        elif args.action == "audit":
            output = audit_completed_review(
                packet_path=args.packet, gold_path=args.gold,
                reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b,
                adjudicator_form=args.adjudicator,
            )
        elif args.action == "run":
            # Review must pass before constructing network/model clients.
            audit_completed_review(
                packet_path=args.packet, gold_path=args.gold,
                reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b,
                adjudicator_form=args.adjudicator,
            )
            backend = _instantiate(args)
            output = run_reviewed(
                packet_path=args.packet, gold_path=args.gold,
                reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b,
                adjudicator_form=args.adjudicator,
                backend=backend, backend_name=args.backend,
                model_dir=args.model_dir, model_name=args.model,
                repetitions=args.repetitions,
            )
        else:
            output = compare_reviewed(
                packet_path=args.packet, gold_path=args.gold,
                reviewer_a=args.reviewer_a, reviewer_b=args.reviewer_b,
                adjudicator_form=args.adjudicator, reports=args.reports,
            )
        _write_once(args.report, output)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        parser.error("missing or invalid human review evidence or local model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
