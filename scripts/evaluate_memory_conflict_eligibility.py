#!/usr/bin/env python3
"""MVP-02C.11 offline metadata-gate contract evaluation (NO model inference).

Selected fixtures were authored WITH knowledge of the provisional v2 gold
labels. Agreement measures typed-policy/fixture consistency only, never
free-text interpretation accuracy or production memory safety.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from persona_agent.memory_conflict_eligibility import (
    NewClaim, PriorClaimSnapshot, evaluate_conflict_eligibility,
)

VERSION = "mvp02c11.structured_eligibility_fixture.v1"
SUITE_VERSION = "mvp02c10.memory_conflict_diagnostic.v2"
REQUIRED_CASE = frozenset(("id", "prior", "candidate", "expected_disposition"))
PRIOR_FIELDS = frozenset(("admission", "memory_kind", "scope"))
CANDIDATE_FIELDS = frozenset((
    "memory_kind", "scope", "statement_type", "modality", "pragmatic_function",
))
DISPOSITIONS = frozenset((
    "NLI_CANDIDATE", "CORRECTION_PATH", "TENTATIVE_RESOLUTION", "SKIP", "ABSTAIN",
))


def read_bounded_json(path: Path) -> tuple[dict, str]:
    data = path.read_bytes()
    if len(data) > 110_000:
        raise ValueError(f"file too large: {path}")
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return obj, sha256(data).hexdigest()


def evaluate_fixture_set(suite_path: Path, fixture_path: Path) -> dict:
    suite, suite_sha = read_bounded_json(suite_path)
    fixtures, fixtures_sha = read_bounded_json(fixture_path)
    if (suite.get("version") != SUITE_VERSION
            or suite.get("annotation_status") != "DRAFT_POLICY_REVIEWED"):
        raise ValueError("only the provisional dataset v2 is supported")
    if (fixtures.get("version") != VERSION
            or fixtures.get("annotation_status") != "SYNTHETIC_GOLD_AWARE_NOT_INDEPENDENT"
            or fixtures.get("source_suite_sha256") != suite_sha):
        raise ValueError("fixture version, provenance or source SHA mismatch")
    fixture_source = fixtures.get("source_suite_path")
    if (not isinstance(fixture_source, str)
        or (Path(__file__).resolve().parents[1] / fixture_source).resolve()
            != suite_path.resolve()):
        raise ValueError("fixture source suite path mismatch")
    source_cases = suite.get("examples")
    if not isinstance(source_cases, list) or len(source_cases) != 94:
        raise ValueError("v2 suite must contain exactly 94 cases")
    by_id = {case["id"]: case for case in source_cases}
    if len(by_id) != len(source_cases):
        raise ValueError("duplicate source case ID")
    selected = fixtures.get("fixtures")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise ValueError("expected 1..100 structured fixtures")
    seen = set()
    rows = []
    for item in selected:
        if not isinstance(item, dict) or set(item) != REQUIRED_CASE:
            raise ValueError("invalid fixture fields")
        key = item["id"]
        if not isinstance(key, str) or key not in by_id or key in seen:
            raise ValueError("duplicate or missing fixture case ID")
        seen.add(key)
        prior_data, candidate_data = item["prior"], item["candidate"]
        if (not isinstance(prior_data, dict) or set(prior_data) != PRIOR_FIELDS
            or not isinstance(candidate_data, dict)
            or set(candidate_data) != CANDIDATE_FIELDS):
            raise ValueError("invalid typed metadata fixture")
        if item["expected_disposition"] not in DISPOSITIONS:
            raise ValueError("unsupported expected disposition")
        # The following identifiers are explicitly synthetic, not an
        # authentication or an assertion that a Memory record exists.
        user_id = uuid5(NAMESPACE_URL, "mvp02c11-fixture-user")
        instance_id = uuid5(NAMESPACE_URL, "mvp02c11-fixture-persona")
        prior_turn_id = uuid5(NAMESPACE_URL, f"mvp02c11/{key}/prior")
        new_turn_id = uuid5(NAMESPACE_URL, f"mvp02c11/{key}/candidate")
        is_admitted = prior_data["admission"] == "ADMITTED_ASSERTED"
        prior = PriorClaimSnapshot.model_validate({
            **prior_data,
            "user_id": user_id, "instance_id": instance_id,
            "source_turn_id": prior_turn_id,
            "memory_id": uuid5(NAMESPACE_URL, f"mvp02c11/{key}/memory") if is_admitted else None,
            "memory_version": 1 if is_admitted else None,
        })
        candidate = NewClaim.model_validate({
            **candidate_data,
            "user_id": user_id, "instance_id": instance_id,
            "source_turn_id": new_turn_id,
            "value_text": by_id[key]["candidate_text"],
        })
        got = evaluate_conflict_eligibility(prior, candidate)
        oracle = by_id[key]
        predicted_eligibility = got.disposition in ("NLI_CANDIDATE", "CORRECTION_PATH")
        rows.append({
            "id": key,
            "decision": got.model_dump(),
            "expected_disposition": item["expected_disposition"],
            "disposition_matches_fixture": got.disposition == item["expected_disposition"],
            "oracle_scope_compatible": oracle["scope_compatible"],
            "scope_matches_oracle": got.scope_compatible is oracle["scope_compatible"],
            "oracle_memory_conflict_eligible": oracle["memory_conflict_eligible"],
            "eligibility_matches_oracle": predicted_eligibility is oracle["memory_conflict_eligible"],
            "oracle_resolution": oracle["expected_resolution"],
        })
    return {
        "version": "mvp02c11.eligibility_fixture_report.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite_version": suite["version"], "suite_sha256": suite_sha,
        "fixture_version": fixtures["version"], "fixture_sha256": fixtures_sha,
        "total_selected": len(rows),
        "source_suite_total": len(source_cases),
        "disposition_fixture_agreements": sum(row["disposition_matches_fixture"] for row in rows),
        "scope_oracle_agreements": sum(row["scope_matches_oracle"] for row in rows),
        "eligibility_oracle_agreements": sum(row["eligibility_matches_oracle"] for row in rows),
        "cases": rows,
        "limitations": [
            "Fixtures are gold-aware synthetic structured metadata; this is NOT a semantic extraction benchmark.",
            "Accuracy or precision/recall of a real scope or modality classifier CANNOT be estimated here.",
            "v2 labels are provisional, not independently blinded and adjudicated.",
            "Claim snapshots and IDs are synthetic; no database, authentication or source verification executed.",
            "NLI_CANDIDATE and CORRECTION_PATH are read-only proposals, never Memory authorization.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c10_memory_conflict_diagnostic_v2.json"))
    parser.add_argument("--fixtures", type=Path, default=Path(
        "scenarios/mvp02c11_structured_eligibility_fixtures.json"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite existing report")
    try:
        result = evaluate_fixture_set(args.suite, args.fixtures)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"Eligibility fixture evaluation failed: {type(exc).__name__}: {exc}\n")
    print(f"Selected {result['total_selected']}/{result['source_suite_total']} cases; "
          f"disposition agreements={result['disposition_fixture_agreements']}; "
          f"scope oracle agreements={result['scope_oracle_agreements']}; "
          f"eligibility oracle agreements={result['eligibility_oracle_agreements']}")
    print(f"Saved {args.report}")
    n = result["total_selected"]
    return 0 if (result["disposition_fixture_agreements"] == n
                 and result["scope_oracle_agreements"] == n
                 and result["eligibility_oracle_agreements"] == n) else 1


if __name__ == "__main__":
    raise SystemExit(main())
