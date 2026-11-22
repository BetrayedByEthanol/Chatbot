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
import json
import os
from pathlib import Path
import re
import secrets
from statistics import mean, median
import subprocess
import sys
from zipfile import ZipFile

from persona_agent.natural_dialogue_review import packet_from_bundle, rating_template
from persona_agent.scenario_validation import validate_scenario

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "scenarios" / "persona_model_comparison_v1.json"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


def parse_models(value: str) -> list[str]:
    """Only bounded model identifiers, never paths or subprocess options."""
    models = [part.strip() for part in value.split(",")]
    if not 2 <= len(models) <= 5 or len(models) != len(set(models)):
        raise ValueError("choose 2..5 distinct model identifiers")
    if any(not MODEL_PATTERN.fullmatch(model) for model in models):
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


def read_summary(bundle: Path) -> dict:
    """Read only observational data already in the canonical E2E ZIP."""
    with ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        events = json.loads(archive.read("transcript.json"))
        snapshots = json.loads(archive.read("state_snapshots.json"))
        evaluation = json.loads(archive.read("evaluation.json"))
        errors = json.loads(archive.read("errors.json"))
    replies = [item for item in events if item.get("action") == "message"]
    durations = [item["response_elapsed_ms"] for item in replies
                 if isinstance(item.get("response_elapsed_ms"), (int, float))]
    word_counts = [len(item.get("assistant", "").split()) for item in replies]
    routes = Counter(item.get("model_id") or "unidentified" for item in replies)
    last = snapshots[-1] if snapshots else {}
    return {
        "contract_passed": bool(evaluation.get("contract_passed")),
        "behavioral_checks_passed": evaluation.get("behavioral_checks_passed"),
        "error_count": len(errors),
        "delivered_message_count": len(replies),
        "avg_response_ms": round(mean(durations), 2) if durations else None,
        "median_response_ms": round(median(durations), 2) if durations else None,
        "avg_reply_words": round(mean(word_counts), 2) if word_counts else None,
        "max_reply_words": max(word_counts) if word_counts else None,
        "model_route_counts": dict(sorted(routes.items())),
        "last_scoped_counts": last.get("application", {}).get("counts", {}),
        "model_identifier_recorded_in_bundle": bool(manifest.get("model")),
        "output_tokens": None,  # The current gateway does not record token usage.
        "quality_status": "AWAITING_INDEPENDENT_HUMAN_REVIEW",
    }


def build_command(mode: str, model: str, scenario: Path, bundle: Path,
                  ollama_url: str | None, direct: bool) -> list[str]:
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
            "model_profiles": "gateway defaults; no per-model prompt overrides",
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
                                args.ollama_url, args.direct)
        print(f"Running {alias} ({mode}, {model}); {len(scenarios[mode]['steps'])} steps",
              flush=True)
        try:
            process = subprocess.run(command, cwd=ROOT, check=False)
            code = process.returncode
        except OSError as exc:
            code = 127
            print(f"{alias} could not launch: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        entry = {"case_id": alias, "mode": mode, "exit_code": code,
                 "bundle": bundle.name if bundle.exists() else None,
                 "review_packet": None,
                 "review_status": "NO_PACKET"}
        if bundle.is_file():
            try:
                entry.update(read_summary(bundle))
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
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                entry["evidence_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcomes.append(entry)
        # This is a private diagnostic, not a ranking or automatic evaluator.
        private_json(destination / f"{alias}.result.json", entry)
    summary = {
        "format": "persona-model-comparison.v1",
        "scenario": source["name"],
        "case_order": list(mapping),
        "results": outcomes,
        "evaluation": "Unscored. Two independent humans must rate blind packets.",
        "quality_winner": None,
    }
    private_json(destination / "summary.json", summary)
    print(f"Private evidence: {destination}", flush=True)
    print("Blind review packets: case_XX.review.json; never give reviewers PRIVATE_model_map.json")
    return 0 if all(item["exit_code"] == 0 and item["review_packet"]
                    for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(compare())
