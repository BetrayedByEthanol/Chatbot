#!/usr/bin/env python3
"""MVP-02C.14: opt-in offline adaptive semantic interpretation evaluation.

An explicit --model enables Ollama inference on selected corpus examples.
Neither model outputs nor author-draft labels authorize Memory mutations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from uuid import NAMESPACE_URL, uuid5

from evaluate_semantic_scope import DIMENSIONS, _confusion, extract_scope, load_corpus
from persona_agent.semantic_adaptive_evaluation import (
    JsonScopeGateway, OllamaScopeGateway, interpret_adaptive,
)


def _summary(rows: list[dict], prefix: str) -> dict:
    field = prefix + "_scope_emitted"
    shadow_rows = [{
        "scope_emitted": row[field],
        "draft_narrow_current_scope": row["draft_narrow_current_scope"],
    } for row in rows]
    return {
        "scope_emissions": sum(row[field] for row in rows),
        "narrow_confusion": _confusion(shadow_rows),
    }


def evaluate_adaptive_cases(
    rows: list[dict], *, gateway: JsonScopeGateway, max_model_calls: int,
) -> dict:
    """Compare the actual deterministic baseline with selective model output.

    Input labels are for scoring only. The model gateway and per-case route
    never see draft labels or group tags; no labels may become authority.
    """
    if type(max_model_calls) is not int or not 0 <= max_model_calls <= 250:
        raise ValueError("invalid inference budget")
    evaluated = []
    attempted = 0
    for row in rows:
        baseline = extract_scope(row["text"], row["id"])
        # Preserve useful baselines when a call budget is exhausted. No
        # fallback model, retry, or automatic request beyond the budget.
        actual_gateway = gateway if attempted < max_model_calls else None
        output = interpret_adaptive(
            row["text"], source_turn_id=uuid5(NAMESPACE_URL, f"mvp02c14/{row['id']}"),
            baseline=baseline, gateway=actual_gateway,
        )
        if output["model_attempted"]:
            attempted += 1
        comparisons = {
            key: {
                "predicted": None if output["labels"] is None else output["labels"][key],
                "match": (output["labels"] is not None
                          and output["labels"][key] is not None
                          and output["labels"][key] == row["draft"][key]),
            } for key in DIMENSIONS
        }
        evaluated.append({
            "id": row["id"], "group": row["group"],
            "route": output["route"], "status": output["status"],
            "reason": output["reason"],
            "baseline_status": baseline["status"],
            "baseline_scope_emitted": baseline["narrow_scope"] is not None,
            "adaptive_scope_emitted": output["narrow_scope"] is not None,
            "draft_narrow_current_scope": row["draft"]["narrow_current_scope"],
            "model_attempted": output["model_attempted"],
            "raw_model_scope": output["raw_model_scope"],
            "model_latency_ms": output["model_latency_ms"],
            "fields": comparisons,
        })
    groups = sorted({row["group"] for row in evaluated})
    elapsed = sorted(row["model_latency_ms"] for row in evaluated
                     if row["model_latency_ms"] is not None)
    by_dim = {
        key: {
            "supported": sum(row["fields"][key]["predicted"] is not None for row in evaluated),
            "correct": sum(row["fields"][key]["match"] for row in evaluated),
            "incorrect": sum(row["fields"][key]["predicted"] is not None
                             and not row["fields"][key]["match"] for row in evaluated),
            "abstained": sum(row["fields"][key]["predicted"] is None for row in evaluated),
        }
        for key in DIMENSIONS
    }
    return {
        "total": len(evaluated),
        "baseline": _summary(evaluated, "baseline"),
        "adaptive": _summary(evaluated, "adaptive"),
        "model_calls": attempted,
        "model_skipped": sum(not row["model_attempted"] for row in evaluated),
        "model_rejected": sum(row["status"] == "MODEL_REJECTED" for row in evaluated),
        "model_errors": sum(row["status"] == "MODEL_ERROR" for row in evaluated),
        "model_abstentions": sum(row["status"] == "MODEL_ABSTAINED" for row in evaluated),
        "model_scope_vetoes": sum(row["status"] == "SCOPE_VETOED" for row in evaluated),
        "status_counts": dict(sorted(Counter(row["status"] for row in evaluated).items())),
        "mean_model_latency_ms": (
            round(sum(elapsed)/len(elapsed), 3) if elapsed else None
        ),
        "median_model_latency_ms": median(elapsed) if elapsed else None,
        "p95_model_latency_ms": (
            elapsed[min(len(elapsed)-1, (95*len(elapsed)+99)//100-1)]
            if elapsed else None
        ),
        "by_dimension": by_dim,
        "by_group": {
            group: {
                "total": len(subset),
                "model_calls": sum(row["model_attempted"] for row in subset),
                "baseline": _summary(subset, "baseline"),
                "adaptive": _summary(subset, "adaptive"),
                "scope_vetoes": sum(row["status"] == "SCOPE_VETOED" for row in subset),
            }
            for group in groups
            for subset in ([row for row in evaluated if row["group"] == group],)
        },
        "new_true_scope": [
            row["id"] for row in evaluated
            if (row["adaptive_scope_emitted"] and not row["baseline_scope_emitted"]
                and row["draft_narrow_current_scope"])
        ],
        "new_false_scope": [
            row["id"] for row in evaluated
            if row["adaptive_scope_emitted"] and not row["draft_narrow_current_scope"]
        ],
        "missed_supported_scope": [
            row["id"] for row in evaluated
            if not row["adaptive_scope_emitted"] and row["draft_narrow_current_scope"]
        ],
        "cases": evaluated,
    }


def _write_once(path: Path, value: dict):
    if path.exists():
        raise ValueError("refusing to overwrite existing evaluation report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c13_semantic_scope_corpus.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--model", required=True,
                        help="Local Ollama model name; model inference is opt-in")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-model-calls", type=int, default=80)
    parser.add_argument("--case-ids", nargs="+",
                        help="Optional explicit case subset for smoke tests")
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite existing evaluation report")
    if not 1 <= args.max_model_calls <= 250:
        parser.error("max-model-calls must be 1..250")
    try:
        rows, digest = load_corpus(args.suite)
        if args.case_ids:
            selected = set(args.case_ids)
            if len(selected) != len(args.case_ids) or not selected.issubset(
                {row["id"] for row in rows}
            ):
                raise ValueError("duplicate or unknown case ID")
            rows = [row for row in rows if row["id"] in selected]
        gateway = OllamaScopeGateway(
            model_id=args.model, base_url=args.base_url,
            timeout_seconds=args.timeout,
        )
        summary = evaluate_adaptive_cases(
            rows, gateway=gateway, max_model_calls=args.max_model_calls,
        )
        output = {
            "version": "mvp02c14.adaptive_semantic_evaluation.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "suite_version": "mvp02c13.semantic_scope_corpus.v1",
            "source_sha256": digest,
            "annotation_status": "DRAFT_AUTHOR_LABELED_NOT_INDEPENDENT",
            "model_id": args.model,
            "prompt_version": "mvp02c14.prompt.v1",
            "routing": "existing deterministic complete scope first, otherwise model on activity cue",
            "full_corpus": not bool(args.case_ids),
            "model_call_budget": args.max_model_calls,
            "metrics": summary,
            "limits": [
                "Author-created synthetic draft labels are NOT independently reviewed gold.",
                "The same corpus was constructed knowing the deterministic baseline.",
                "Model-generated scope is advisory only; no source truth or Memory authority is established.",
                "Lexical risk vetoes are conservative heuristics, not an independently validated policy.",
                "Errors are visible, not retried; partial calls and selection bias affect comparisons.",
                "No PostgreSQL, user conversations, new Memory records or domain state were involved.",
                "Do not tune and report on this corpus as an unseen holdout.",
            ],
        }
        _write_once(args.report, output)
    except (ValueError, TypeError, OSError) as exc:
        parser.exit(2, f"Adaptive semantic evaluation failed: {type(exc).__name__}: {exc}\n")
    m = summary
    print(f"{m['total']} cases; {m['model_calls']} model calls; "
          f"baseline TP/FP/FN={m['baseline']['narrow_confusion']['tp']}/"
          f"{m['baseline']['narrow_confusion']['fp']}/"
          f"{m['baseline']['narrow_confusion']['fn']}; "
          f"adaptive TP/FP/FN={m['adaptive']['narrow_confusion']['tp']}/"
          f"{m['adaptive']['narrow_confusion']['fp']}/"
          f"{m['adaptive']['narrow_confusion']['fn']}")
    print(f"Saved {args.report}")
    return 1 if summary["model_errors"] or summary["model_rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
