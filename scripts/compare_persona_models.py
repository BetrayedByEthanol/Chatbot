#!/usr/bin/env python3
"""Run the same synthetic conversation against selected Ollama models.

Reuses persona_e2e.py, run_persona_e2e.sh, and MVP-03C blind human review.
Each model/mode pair starts with a new, isolated persona database. No quality
score is inferred from response text; independent human ratings remain required.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import file_digest
import json
from math import ceil, isfinite
import os
from pathlib import Path
import re
import secrets
from statistics import mean, median
import subprocess
import sys
from zipfile import BadZipFile, ZipFile

from persona_agent.natural_dialogue_review import (
    packet_from_bundle, rating_template, summarize_independent_reviews,
    validate_packet, validate_ratings,
)
from persona_agent.scenario_validation import validate_scenario

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "scenarios" / "persona_model_comparison_v1.json"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


def parse_models(value: str) -> list[str]:
    """Only bounded model identifiers, never paths or subprocess options."""
    models = [part.strip() for part in value.split(",")]
    if not 2 <= len(models) <= 5 or len(models) != len(set(models)):
        raise ValueError("choose 2..5 distinct model identifiers")
    if any(not MODEL_PATTERN.fullmatch(model) or ".." in model for model in models):
        raise ValueError("invalid model identifier")
    return models


def variant(source: dict, mode: str) -> dict:
    """Keep text, order, rubric, sessions and restart positions identical."""
    if mode not in ("model-only", "full"):
        raise ValueError("unknown comparison mode")
    clone = deepcopy(source)
    clone["name"] = source["name"] + ("-model-only" if mode == "model-only" else "-full")
    for step in clone["steps"]:
        if step["action"] == "message":
            step["mode"] = "model" if mode == "model-only" else "auto"
    return validate_scenario(clone)


def private_json(path: Path, obj: object) -> None:
    """Create-only, 0600 JSON; never overwrite another report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(obj, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())



def _failure_diagnostics(errors: list) -> list[dict]:
    """Only safe classifications; raw provider errors stay in the private ZIP."""
    diagnostics = []
    for item in errors[:20]:
        if not isinstance(item, dict):
            raise ValueError("invalid E2E error record")
        kind = item.get("type")
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind):
            kind = "UnknownError"
        step = item.get("step_index")
        row = {"step_index": step if type(step) is int and step >= 0 else None,
               "type": kind}
        if kind == "ModelUnavailable":
            message = item.get("message")
            # The adapter deliberately uses bounded, source-free messages.
            if isinstance(message, str) and re.fullmatch(
                r"model gateway request failed \((?:HTTP [1-5][0-9]{2}|HTTP error|timeout|"
                r"connection refused|connection reset|DNS resolution failed|"
                r"HTTP protocol failure|transport failure)\)", message
            ):
                row["model_failure"] = message
            elif message in (
                "model gateway response exceeds size limit",
                "model gateway returned invalid response",
                "invalid structured model reply", "oversized plain model fallback",
                "structured model reply has no response text",
                "structured model reply must be an object",
                "structured model reply is oversized",
            ):
                row["model_failure"] = message
            else:
                row["model_failure"] = "unclassified model failure"
        diagnostics.append(row)
    return diagnostics


def read_summary(bundle: Path) -> dict:
    """Read only observational data already in the canonical E2E ZIP."""
    with ZipFile(bundle) as archive:
        # An incomplete but parseable ZIP is not a published E2E bundle.
        # Validate the canonical members and all CRCs before permitting
        # another model to start against the shared local Compose port.
        required = {
            "manifest.json", "scenario.json", "transcript.json",
            "state_snapshots.json", "model_requests.json",
            "interpretation_diagnostics.json", "semantic_diagnostics.json",
            "review_items.json", "delivery_events.json",
            "evaluation.json", "errors.json",
        }
        names = archive.namelist()
        if len(names) != len(set(names)) or not required <= set(names):
            raise ValueError("incomplete or duplicate E2E bundle members")
        if archive.testzip() is not None:
            raise ValueError("E2E evidence ZIP failed CRC integrity check")
        manifest = json.loads(archive.read("manifest.json"))
        scenario = json.loads(archive.read("scenario.json"))
        events = json.loads(archive.read("transcript.json"))
        snapshots = json.loads(archive.read("state_snapshots.json"))
        review_items = json.loads(archive.read("review_items.json"))
        evaluation = json.loads(archive.read("evaluation.json"))
        errors = json.loads(archive.read("errors.json"))
    # A present pathname is not evidence of successful publication. Only a
    # readable canonical synthetic E2E ZIP with the required records counts.
    if (not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str)
        or not isinstance(review_items, list)
        or not isinstance(manifest, dict) or manifest.get("synthetic_data") is not True
        or not isinstance(manifest.get("model"), str)
        or not manifest["model"]
        or not isinstance(events, list)
        or not isinstance(snapshots, list)
        or not isinstance(errors, list)
        or not isinstance(evaluation, dict)
        or type(evaluation.get("contract_passed")) is not bool):
        raise ValueError("invalid or incomplete published E2E evidence")
    replies = [item for item in events if item.get("action") == "message"]
    durations = [item["response_elapsed_ms"] for item in replies
                 if type(item.get("response_elapsed_ms")) in (int, float)
                 and isfinite(item["response_elapsed_ms"]) and item["response_elapsed_ms"] >= 0]
    word_counts = [len(item.get("assistant", "").split()) for item in replies]
    routes = Counter(item.get("model_id") or "unidentified" for item in replies)
    last = snapshots[-1] if snapshots else {}
    with bundle.open("rb") as stream:
        bundle_sha256 = file_digest(stream, "sha256").hexdigest()
    return {
        "bundle_sha256": bundle_sha256,
        "bundle_model": manifest["model"],
        "bundle_scenario": scenario["name"],
        "git_sha": manifest.get("git_sha"),
        "failure_diagnostics": _failure_diagnostics(errors),
        "contract_passed": bool(evaluation.get("contract_passed")),
        "behavioral_checks_passed": evaluation.get("behavioral_checks_passed"),
        "error_count": len(errors),
        "delivered_message_count": len(replies),
        "avg_response_ms": round(mean(durations), 2) if durations else None,
        "median_response_ms": round(median(durations), 2) if durations else None,
        "p95_response_ms": round(sorted(durations)[ceil(.95 * len(durations)) - 1], 2)
                           if durations else None,
        "timed_message_count": len(durations),
        "avg_reply_words": round(mean(word_counts), 2) if word_counts else None,
        "max_reply_words": max(word_counts) if word_counts else None,
        "model_route_counts": dict(sorted(routes.items())),
        "last_scoped_counts": last.get("application", {}).get("counts", {}),
        "model_identifier_recorded_in_bundle": bool(manifest.get("model")),
        "output_tokens": None,  # The current gateway does not record token usage.
        "quality_status": ("AWAITING_INDEPENDENT_HUMAN_REVIEW"
                           if evaluation["contract_passed"] and not errors else "NO_PACKET"),
    }


def build_command(mode: str, model: str, scenario: Path, bundle: Path,
                  ollama_url: str | None, direct: bool,
                  thinking: str = "default") -> list[str]:
    command = (
        [sys.executable, "scripts/persona_e2e.py"] if direct else
        ["bash", "scripts/run_persona_e2e.sh"]
    )
    command += [
        "--scenario", str(scenario),
        "--model", model,
        "--report", str(bundle),
    ]
    if ollama_url:
        command += ["--ollama-url", ollama_url]
    if thinking != "default":
        command += ["--thinking", thinking]
    return command


def compare(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--models", required=True, help="Comma-separated 2..5 Ollama model IDs")
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--mode", choices=("model-only", "full", "both"),
                        default="model-only")
    parser.add_argument("--output-dir", type=Path,
                        help="New, private results directory; never overwritten")
    parser.add_argument("--ollama-url", help="Override the same Ollama URL for all runs")
    parser.add_argument("--thinking", choices=("default", "on", "off"),
                        default="default",
                        help="Set Ollama think=true/false for every model run; default leaves it unset")
    parser.add_argument("--direct", action="store_true",
                        help="Use configured disposable PostgreSQL via persona_e2e.py; no Compose")
    args = parser.parse_args(argv)
    try:
        models = parse_models(args.models)
        source = validate_scenario(json.loads(args.scenario.read_text(encoding="utf-8")))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.direct and not os.getenv("PERSONA_E2E_ADMIN_DSN"):
        parser.error("--direct requires PERSONA_E2E_ADMIN_DSN for an isolated PostgreSQL server")
    modes = ["model-only", "full"] if args.mode == "both" else [args.mode]
    scenarios = {mode: variant(source, mode) for mode in modes}

    destination = (args.output_dir or (
        ROOT / "artifacts" /
        ("model-comparison-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
         + "-" + secrets.token_hex(4))
    )).resolve()
    # All evidence includes synthetic persona transcript, prompts and state.
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    if destination.stat().st_mode & 0o077:
        raise PermissionError("comparison directory must not allow group/other access")
    os.chmod(destination, 0o700)
    jobs = [(model, mode) for mode in modes for model in models]
    secrets.SystemRandom().shuffle(jobs)  # Review aliases hide model ordering.
    mapping = {
        f"case_{i:02d}": {"model": model, "mode": mode}
        for i, (model, mode) in enumerate(jobs, 1)
    }
    private_json(destination / "PRIVATE_model_map.json", {
        "format": "persona-model-comparison.v1",
        "scenario": source["name"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mapping": mapping,
        "gateway_configuration": {
            "ollama_url": args.ollama_url or os.getenv("ACCEPTANCE_OLLAMA_URL",
                                                     "http://127.0.0.1:11434"),
            "model_profiles": "gateway defaults except explicit global thinking mode",
            "thinking": args.thinking,
            "token_usage": "not captured by this gateway",
        },
        "warnings": [
            "Contains model identifiers and paths to sensitive local test artifacts",
            "Only the case_XX.review.json files should be shared with blind reviewers",
        ],
    })
    outcomes = []
    for alias, identity in mapping.items():
        model, mode = identity["model"], identity["mode"]
        scenario_path = destination / f"{alias}.scenario.json"
        bundle = destination / f"{alias}.zip"
        private_json(scenario_path, scenarios[mode])
        command = build_command(mode, model, scenario_path, bundle,
                                args.ollama_url, args.direct, args.thinking)
        print(f"Running {alias} ({mode}, {model}); {len(scenarios[mode]['steps'])} steps",
              flush=True)
        try:
            process = subprocess.run(command, cwd=ROOT, check=False)
            code = process.returncode
        except OSError as exc:
            code = 127
            print(f"{alias} could not launch: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        entry = {"case_id": alias, "mode": mode, "thinking": args.thinking,
                 "exit_code": code,
                 "bundle": bundle.name if bundle.exists() else None,
                 "review_packet": None,
                 "review_status": "NO_PACKET"}
        evidence_verified = False
        if bundle.is_file():
            try:
                entry.update(read_summary(bundle))
                evidence_verified = True
                entry["evidence_status"] = "VERIFIED"
                entry["quality_status"] = "NO_PACKET"
                # A failed contract trace must never be rated as a successful
                # natural-dialogue run, even if it contains plausible replies.
                if code == 0 and entry["contract_passed"] and not entry["error_count"]:
                    packet = packet_from_bundle(bundle)
                    private_json(destination / f"{alias}.review.json", packet)
                    private_json(destination / f"{alias}.reviewer_a.json",
                                 rating_template(packet, "reviewer_a"))
                    private_json(destination / f"{alias}.reviewer_b.json",
                                 rating_template(packet, "reviewer_b"))
                    entry["review_packet"] = f"{alias}.review.json"
                    entry["review_status"] = "AWAITING_INDEPENDENT_HUMAN_REVIEW"
                    entry["quality_status"] = entry["review_status"]
            except (ValueError, OSError, KeyError, TypeError, RuntimeError, BadZipFile) as exc:
                entry["evidence_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if not evidence_verified:
            entry["evidence_status"] = "INVALID" if bundle.exists() else "MISSING"
        outcomes.append(entry)
        # This is a private diagnostic, not a ranking or automatic evaluator.
        private_json(destination / f"{alias}.result.json", entry)
        if code not in (0, 1) or not evidence_verified:
            # Status 1 is a valid contract-failure result ONLY with a complete,
            # readable evidence ZIP. Otherwise the E2E wrapper may have kept
            # its PostgreSQL service on the fixed port: stop before the next
            # model and retain whatever partial diagnostic evidence exists.
            print(
                f"{alias}: infrastructure or unconfirmed evidence publication "
                f"(status={code}, evidence={entry['evidence_status']}); "
                "aborting remaining jobs",
                file=sys.stderr,
            )
            break
    summary = {
        "format": "persona-model-comparison.v1",
        "scenario": source["name"],
        "case_order": list(mapping),
        "results": outcomes,
        "not_run_case_ids": list(mapping)[len(outcomes):],
        "evaluation": "Unscored. Two independent humans must rate blind packets.",
        "quality_winner": None,
    }
    private_json(destination / "summary.json", summary)
    print(f"Private evidence: {destination}", flush=True)
    print("Blind review packets: case_XX.review.json; never give reviewers PRIVATE_model_map.json")
    return 0 if len(outcomes) == len(mapping) and all(
        item["exit_code"] == 0 and item["review_packet"] for item in outcomes
    ) else 1


def comparison_review_status(directory: Path) -> dict:
    """Read-only operator report over an existing, private comparison directory.

    Reconcile original run metadata with evidence and recompute review summaries
    from scorecards, never trust a pre-existing aggregate or select a winner.
    Legacy v1 bundles without recorded hashes get an explicit provenance warning.
    """
    def load(name: str):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    summary = load("summary.json")
    model_map = load("PRIVATE_model_map.json")
    if (not isinstance(summary, dict) or not isinstance(model_map, dict)
            or summary.get("format") != "persona-model-comparison.v1"
            or model_map.get("format") != summary["format"]
            or not isinstance(summary.get("scenario"), str)
            or model_map.get("scenario") != summary["scenario"]):
        raise ValueError("invalid or mismatched comparison metadata")
    order, results, mapping = (summary.get("case_order"), summary.get("results"),
                               model_map.get("mapping"))
    if (not isinstance(order, list) or not 2 <= len(order) <= 10
            or any(not isinstance(a, str) or not re.fullmatch(r"case_[0-9]{2}", a) for a in order)
            or len(set(order)) != len(order) or not isinstance(mapping, dict)
            or set(mapping) != set(order) or not isinstance(results, list)
            or len(results) > len(order)
            or any(not isinstance(r, dict) for r in results)
            or [r.get("case_id") for r in results] != order[:len(results)]):
        raise ValueError("invalid comparison case inventory")
    cases = []
    for index, alias in enumerate(order):
        identity = mapping[alias]
        if (not isinstance(identity, dict) or identity.get("mode") not in ("model-only", "full")
                or not isinstance(identity.get("model"), str)
                or not MODEL_PATTERN.fullmatch(identity["model"])):
            raise ValueError(f"{alias}: invalid private model mapping")
        row = {"case_id": alias, "model": identity["model"], "mode": identity["mode"],
               "status": "NOT_RUN", "natural_quality_accepted": None}
        cases.append(row)
        if index >= len(results):
            continue
        recorded = results[index]
        if recorded.get("mode") != identity["mode"]:
            row.update(status="EVIDENCE_MISMATCH", diagnostic="recorded route differs from model map")
            continue
        row["exit_code"] = recorded.get("exit_code")
        bundle = directory / f"{alias}.zip"
        try:
            actual = read_summary(bundle)
        except (ValueError, OSError, TypeError, KeyError, RuntimeError, BadZipFile) as exc:
            row.update(status="EVIDENCE_MISSING_OR_INVALID",
                       diagnostic=f"bundle verification failed: {type(exc).__name__}")
            continue
        row["observed"] = actual
        expected_scenario = summary["scenario"] + "-" + identity["mode"]
        mismatches = [k for k in (
            "contract_passed", "error_count", "delivered_message_count", "behavioral_checks_passed",
            "bundle_sha256", "git_sha",
        ) if k in recorded and recorded[k] != actual[k]]
        if recorded.get("bundle") != bundle.name:
            mismatches.append("bundle filename")
        if actual["bundle_model"] != identity["model"]:
            mismatches.append("model")
        if actual["bundle_scenario"] != expected_scenario:
            mismatches.append("scenario")
        if mismatches:
            row.update(status="EVIDENCE_MISMATCH",
                       diagnostic="bundle differs from recorded run: " + ", ".join(mismatches))
            continue
        row["run_fingerprint"] = ("MATCHED" if recorded.get("bundle_sha256")
                                  else "LEGACY_HASH_UNAVAILABLE")
        if (recorded.get("exit_code") != 0 or not actual["contract_passed"]
                or actual["error_count"] or actual["behavioral_checks_passed"] is False):
            row["status"] = "RUN_FAILED"
            continue
        if recorded.get("review_packet") != f"{alias}.review.json":
            row.update(status="REVIEW_PACKET_MISSING",
                       diagnostic="successful run has no recorded review packet")
            continue
        try:
            packet = validate_packet(load(f"{alias}.review.json"))
            if packet["packet_sha256"] != packet_from_bundle(bundle)["packet_sha256"]:
                raise ValueError("packet does not match bundle")
        except (ValueError, OSError, TypeError, KeyError, RuntimeError, BadZipFile) as exc:
            row.update(status="REVIEW_PACKET_INVALID",
                       diagnostic=f"packet verification failed: {type(exc).__name__}")
            continue
        slots = {}
        submissions = []
        for slot in ("a", "b"):
            try:
                submission = validate_ratings(packet, load(f"{alias}.reviewer_{slot}.json"))
            except FileNotFoundError:
                slots[slot] = {"status": "MISSING"}
            except (ValueError, OSError, TypeError, KeyError) as exc:
                # Detailed validation errors aid local reviewers; do not echo notes,
                # raw prompts or arbitrary OS error messages into this report.
                slots[slot] = {"status": "INCOMPLETE_OR_INVALID",
                               "diagnostic": str(exc)[:500] if isinstance(exc, ValueError)
                               else type(exc).__name__}
            else:
                slots[slot] = {"status": "VALID"}
                submissions.append(submission)
        row["reviewer_slots"] = slots
        if len(submissions) != 2:
            row["status"] = "REVIEW_INCOMPLETE_OR_INVALID"
            continue
        try:
            aggregate = summarize_independent_reviews(packet, *submissions)
        except ValueError as exc:
            row.update(status="REVIEW_INCOMPLETE_OR_INVALID", diagnostic=str(exc)[:500])
            continue
        row.update(status=aggregate["status"], review_summary={
            "rated_dimensions": aggregate["rated_dimensions"],
            "exact_agreement_rate": aggregate["exact_agreement_rate"],
            "dimensions": aggregate["dimensions"],
            "disagreement_count": len(aggregate["disagreements"]),
            "critical_flag_count": len(aggregate["critical_flags"]),
        })
    return {
        "format": "persona-model-comparison-review-status.v1",
        "scenario": summary["scenario"], "cases": cases,
        "status_counts": dict(sorted(Counter(r["status"] for r in cases).items())),
        "quality_winner": None, "natural_quality_accepted": None,
        "note": "Private operator diagnostics only. Valid submissions do not prove human independence.",
    }


if __name__ == "__main__":
    raise SystemExit(compare())
