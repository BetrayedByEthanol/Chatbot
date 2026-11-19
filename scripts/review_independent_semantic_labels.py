#!/usr/bin/env python3
"""MVP-02C.14J: human-only independent semantic annotation and adjudication.

No automatic semantic labeling. A signed human review process is attested,
not cryptographically proven; the packet itself has NO semantic gold.
Resulting reviewed source+labels are local-only and MUST NOT be committed.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from evaluate_semantic_models import LABEL_FIELDS, ProbeLabels

PACKET_VERSION = "mvp02c14j.blind_claim_packet.v1"
FORM_VERSION = "mvp02c14j.human_annotation_form.v1"
GOLD_VERSION = "mvp02c14j.adjudicated_claim_reference.v1"
GOLD_STATUS = "HUMAN_REVIEW_ATTESTED_NOT_INDEPENDENTLY_VERIFIED"
LABELS_UNREVIEWED = "UNLABELED_REVIEW_PACKET"
REQUIRED_ATTESTATIONS = {
    "reviewer": ("annotated_independently", "no_model_outputs_seen",
                 "no_other_review_submissions_seen"),
    "adjudicator": ("personally_adjudicated_all_claims", "no_model_outputs_seen",
                    "reviewed_two_independent_submissions"),
}


def _load(path: Path) -> tuple[dict, str]:
    blob = path.read_bytes()
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data, sha256(blob).hexdigest()


def _write_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def _identity(value, name: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or value != value.strip() or len(value) > 80
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"invalid {name}")
    return value


def load_packet(path: Path) -> tuple[dict, str, list[tuple[str, int]]]:
    packet, digest = _load(path)
    if (packet.get("version") != PACKET_VERSION
            or packet.get("annotation_status") != LABELS_UNREVIEWED
            or packet.get("independent_labels_available") is not False
            or packet.get("scope") != "OFFLINE_ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY"
            or not isinstance(packet.get("cases"), list)
            or not packet["cases"]):
        raise ValueError("not an unlabeled v14J claim packet")
    keys = []
    cases = set()
    for case in packet["cases"]:
        if not isinstance(case, dict) or set(case) != {"id", "text", "claims"}:
            raise ValueError("invalid packet case")
        cid, source = case["id"], case["text"]
        if (not isinstance(cid, str) or not cid or cid in cases
                or not isinstance(source, str) or not source
                or not isinstance(case["claims"], list)
                or not 1 <= len(case["claims"]) <= 8):
            raise ValueError("invalid packet case identity or claim count")
        cases.add(cid)
        spans = set()
        for index, claim in enumerate(case["claims"]):
            if not isinstance(claim, dict) or set(claim) != {"start", "end", "text"}:
                raise ValueError("invalid oracle claim span")
            start, end, quote = claim["start"], claim["end"], claim["text"]
            if (type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end > len(source)
                    or not isinstance(quote, str)
                    or source[start:end] != quote
                    or (start, end) in spans):
                raise ValueError("claim evidence does not match full source")
            spans.add((start, end))
            keys.append((cid, index))
    return packet, digest, keys


def make_form(*, packet_path: Path, actor_id: str, role: str) -> dict:
    if role not in REQUIRED_ATTESTATIONS:
        raise ValueError("invalid review role")
    packet, digest, keys = load_packet(packet_path)
    return {
        "version": FORM_VERSION, "role": role, "actor_id": _identity(actor_id, "actor ID"),
        "packet_sha256": digest,
        "attestations": {name: False for name in REQUIRED_ATTESTATIONS[role]},
        "items": [{"case_id": cid, "claim_index": index, "labels": None,
                   "rationale": ""} for cid, index in keys],
        "instructions": (
            "Human-only: read the blind packet and fill ALL seven dimension labels "
            "using allowed values. Mark attestations true only if accurate. "
            "No model or assistant may fill labels. An adjudicator must submit "
            "their own completed decisions; disagreements require rationale."
        ),
        "unreviewed_case_count": len(packet["cases"]),
    }


def _validate_form(form: dict, *, role: str, digest: str,
                   keys: list[tuple[str, int]]) -> tuple[str, dict]:
    if (form.get("version") != FORM_VERSION or form.get("role") != role
            or form.get("packet_sha256") != digest):
        raise ValueError("incorrect review form version, role, or source checksum")
    actor_id = _identity(form.get("actor_id"), "actor ID")
    att = form.get("attestations")
    if (not isinstance(att, dict)
            or set(att) != set(REQUIRED_ATTESTATIONS[role])
            or any(att[k] is not True for k in REQUIRED_ATTESTATIONS[role])):
        raise ValueError("required human independence attestations missing")
    items = form.get("items")
    if not isinstance(items, list) or len(items) != len(keys):
        raise ValueError("incomplete review")
    output = {}
    for expected, item in zip(keys, items):
        if not isinstance(item, dict) or set(item) != {
            "case_id", "claim_index", "labels", "rationale"
        }:
            raise ValueError("invalid item fields")
        if (item["case_id"], item["claim_index"]) != expected or (
            type(item["claim_index"]) is not int
        ):
            raise ValueError("wrong case order or target index")
        labels = item["labels"]
        if not isinstance(labels, dict) or set(labels) != set(LABEL_FIELDS):
            raise ValueError("incomplete seven-dimension human labels")
        parsed = ProbeLabels.from_mapping(labels)
        rationale = item["rationale"]
        if not isinstance(rationale, str) or len(rationale) > 2000:
            raise ValueError("invalid review rationale")
        output[expected] = (parsed.model_dump(mode="json"), rationale)
    return actor_id, output


def adjudicate(*, packet_path: Path, reviewer_a: Path, reviewer_b: Path,
               adjudicator_form: Path) -> dict:
    packet, digest, keys = load_packet(packet_path)
    a, a_sha = _load(reviewer_a)
    b, b_sha = _load(reviewer_b)
    adjudicated, c_sha = _load(adjudicator_form)
    a_id, a_items = _validate_form(a, role="reviewer", digest=digest, keys=keys)
    b_id, b_items = _validate_form(b, role="reviewer", digest=digest, keys=keys)
    c_id, c_items = _validate_form(
        adjudicated, role="adjudicator", digest=digest, keys=keys
    )
    if len({a_id, b_id, c_id}) != 3:
        raise ValueError("reviewers and adjudicator must be distinct actors")

    disagreements = {field: 0 for field in LABEL_FIELDS}
    for key in keys:
        baseline, alternate = a_items[key][0], b_items[key][0]
        for field in LABEL_FIELDS:
            if baseline[field] != alternate[field]:
                disagreements[field] += 1
        if (baseline != alternate and not c_items[key][1].strip()):
            raise ValueError("adjudication rationale required for disagreement")

    cases = []
    for case in packet["cases"]:
        claims = []
        for index, span in enumerate(case["claims"]):
            claims.append({**span, "labels": c_items[(case["id"], index)][0]})
        cases.append({"id": case["id"], "text": case["text"], "claims": claims})
    return {
        "version": GOLD_VERSION,
        "annotation_status": GOLD_STATUS,
        "review_process": "TWO_BLINDED_REVIEWERS_AND_DISTINCT_HUMAN_ADJUDICATOR",
        "independence": "HUMAN_ATTESTED_NOT_TECHNICALLY_VERIFIABLE",
        "reference_mode": "OFFLINE_ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY",
        "source_packet_sha256": digest,
        "review_provenance": {
            "reviewer_a_id": a_id, "reviewer_a_sha256": a_sha,
            "reviewer_b_id": b_id, "reviewer_b_sha256": b_sha,
            "adjudicator_id": c_id, "adjudicator_sha256": c_sha,
        },
        "disagreements_by_dimension": disagreements,
        "claim_count": len(keys),
        "cases": cases,
        "limits": [
            "Independent human review is attested by submitters, not machine-verifiable.",
            "Claim spans are author-suggested and do not establish extraction quality.",
            "No model predictions may be shown to reviewers before adjudication.",
            "This fixture is only a small synthetic pilot, not a deployment gold standard.",
            "This output includes source text: keep local, do NOT commit to repository.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    form = subs.add_parser("form")
    form.add_argument("--packet", type=Path, required=True)
    form.add_argument("--role", choices=tuple(REQUIRED_ATTESTATIONS), required=True)
    form.add_argument("--actor-id", required=True)
    form.add_argument("--output", type=Path, required=True)
    seal = subs.add_parser("adjudicate")
    seal.add_argument("--packet", type=Path, required=True)
    seal.add_argument("--reviewer-a", type=Path, required=True)
    seal.add_argument("--reviewer-b", type=Path, required=True)
    seal.add_argument("--adjudicator", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite existing human review output")
    try:
        if args.action == "form":
            result = make_form(packet_path=args.packet, actor_id=args.actor_id,
                               role=args.role)
        else:
            result = adjudicate(packet_path=args.packet, reviewer_a=args.reviewer_a,
                                reviewer_b=args.reviewer_b,
                                adjudicator_form=args.adjudicator)
        _write_once(args.output, result)
    except (OSError, TypeError, ValueError, KeyError):
        parser.error("invalid review packet, evidence, attestations or output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
