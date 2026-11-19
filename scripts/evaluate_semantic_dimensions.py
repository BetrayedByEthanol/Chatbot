#!/usr/bin/env python3
"""MVP-02C.14D read-only paired evaluation of injected semantic predictions.

Predictions are supplied from an explicit local JSON file, never generated
from reference labels. Reports omit user text and source evidence spans.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from persona_agent.semantic_dimension_contracts import (
    SemanticDimensionsV2, validate_semantic_dimensions,
)
from persona_agent.semantic_dimension_evaluation import evaluate_dimension_pairs

VERSION = "mvp02c14d.semantic_evaluation.v1"
SUITE_VERSION = "mvp02c14d.semantic_drafts.v1"
PREDICTION_VERSION = "mvp02c14d.predictions.v1"
DRAFT_STATUS = "AUTHOR_DRAFT_NOT_INDEPENDENT"


def _read_json(path: Path) -> tuple[dict, str]:
    blob = path.read_bytes()
    value = json.loads(blob)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value, sha256(blob).hexdigest()


def evaluate_files(suite_path: Path, prediction_path: Path) -> dict:
    suite, digest = _read_json(suite_path)
    predictions, prediction_digest = _read_json(prediction_path)
    if suite.get("version") != SUITE_VERSION or suite.get("annotation_status") != DRAFT_STATUS:
        raise ValueError("unexpected suite version or annotation status")
    if predictions.get("version") != PREDICTION_VERSION:
        raise ValueError("unexpected prediction version")
    if not isinstance(suite.get("examples"), list) or not isinstance(
        predictions.get("items"), list
    ):
        raise ValueError("examples and predictions must be lists")
    found = {}
    for item in predictions["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "interpretation"}:
            raise ValueError("invalid prediction item")
        case_id = item["id"]
        if not isinstance(case_id, str) or case_id in found:
            raise ValueError("invalid or duplicate prediction ID")
        found[case_id] = item["interpretation"]

    rows = []
    seen = set()
    for item in suite["examples"]:
        if not isinstance(item, dict) or set(item) != {"id", "text", "reference"}:
            raise ValueError("invalid suite item")
        case_id, source = item["id"], item["text"]
        if not isinstance(case_id, str) or case_id in seen or not isinstance(source, str):
            raise ValueError("duplicate or invalid suite case")
        seen.add(case_id)
        reference = SemanticDimensionsV2.model_validate_json(
            json.dumps(item["reference"])
        )
        validate_semantic_dimensions(
            reference, source_turn_id=reference.source_turn_id, source_text=source
        )
        if case_id in found:
            prediction = SemanticDimensionsV2.model_validate_json(
                json.dumps(found[case_id])
            )
        else:
            prediction = SemanticDimensionsV2(
                source_turn_id=reference.source_turn_id,
                interpreter_id="offline.missing_prediction",
                interpreter_version="1",
                route="DETERMINISTIC",
            )
        validate_semantic_dimensions(
            prediction, source_turn_id=reference.source_turn_id, source_text=source
        )
        rows.append((case_id, reference, prediction))
    if set(found) != seen & set(found):
        raise ValueError("unknown prediction case IDs")

    return {
        "version": VERSION,
        "suite_sha256": digest,
        "predictions_sha256": prediction_digest,
        "annotation_status": DRAFT_STATUS,
        "prediction_count": len(found),
        "missing_predictions": len(rows) - len(found),
        "metrics": evaluate_dimension_pairs(rows),
        "limits": [
            "Provisional author annotations, NOT independently adjudicated gold.",
            "Exact-span alignment is a strict diagnostic, not semantic equivalence.",
            "No inference, Memory writes, model selection, or training admission.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c14d_semantic_drafts.json"))
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite existing report")
    try:
        result = evaluate_files(args.suite, args.predictions)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    except (ValueError, TypeError, OSError, KeyError, json.JSONDecodeError):
        parser.error("invalid or unavailable evaluation inputs; no report written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
