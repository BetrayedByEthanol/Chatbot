#!/usr/bin/env python3
"""MVP-02C.14B — offline, read-only zero-shot semantic-dimension evaluation.

Runs a LOCAL NLI zero-shot model against the existing provisional v2.13
corpus. Scores statement type, time scope, modality and pragmatic function
independently. Never extracts canonical Memory claims or grants write access.

Only the CLI loads optional Transformers/Torch dependencies. Unit tests use
an injected backend so CI requires neither those packages nor a model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Protocol

from evaluate_semantic_scope import (
    DATASET_VERSION, LABEL_STATUS, extract_scope, load_corpus,
)

VERSION = "mvp02c14b.zeroshot_semantic_evaluation.v1"
HYPOTHESES_VERSION = "mvp02c14b.hypotheses.v1"

# Each hypothesis is an independent entailment test, NOT a calibrated
# multiclass probability. Frozen and versioned to permit comparable runs.
HYPOTHESES = {
    "statement_type": {
        "FACT": "The speaker states a factual claim.",
        "PREFERENCE": "The speaker expresses a personal preference.",
        "EXPERIENCE": "The speaker describes an experience.",
        "INTENTION": "The speaker expresses an intention or plan.",
        "OPINION": "The speaker expresses an opinion or judgment.",
        "UNKNOWN": "The statement type of this utterance is unclear.",
    },
    "temporal_scope": {
        "CURRENT": "The speaker describes a situation that currently holds.",
        "HABITUAL": "The speaker describes a present recurring habit.",
        "PAST_EVENT": "The speaker describes an event that happened in the past.",
        "PAST_HABITUAL": "The speaker describes a past habit that no longer holds.",
        "FUTURE": "The speaker describes a future situation or intention.",
        "UNSPECIFIED": "The utterance does not indicate a particular time.",
    },
    "modality": {
        "ASSERTED": "The speaker asserts the proposition as true.",
        "TENTATIVE": "The speaker is uncertain whether the proposition is true.",
        "CONDITIONAL": "The speaker makes the proposition conditional.",
        "HYPOTHETICAL": "The speaker describes a hypothetical possibility.",
        "UNKNOWN": "The speaker's degree of certainty is unclear.",
    },
    "pragmatic_function": {
        "INFORMING": "The speaker is providing information.",
        "REQUESTING": "The speaker asks somebody to perform an action.",
        "CORRECTING": "The speaker is correcting previous information.",
        "ASKING": "The speaker is asking a question to obtain information.",
        "UNKNOWN": "The speaker's communicative purpose is unclear.",
    },
}
assert all(len(set(labels.values())) == len(labels) for labels in HYPOTHESES.values())


class ZeroShotBackend(Protocol):
    """Returns one HF-style label/score mapping per input, in input order."""

    def classify(self, texts: list[str], hypotheses: list[str]) -> list[dict]:
        ...


class LocalTransformersZeroShot:
    """Explicitly local model, loaded once; no Ollama or network fallback."""

    def __init__(self, model_dir: Path, *, device: str):
        model_dir = model_dir.expanduser().resolve(strict=True)
        if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
            raise ValueError("model-dir must be an existing local HF model directory")
        if not (model_dir / "model.safetensors").is_file() and not (
            model_dir / "pytorch_model.bin"
        ).is_file():
            raise ValueError("model-dir requires native weights (ONNX is not used)")
        if not (model_dir / "tokenizer.json").is_file() and not (
            model_dir / "spm.model"
        ).is_file():
            raise ValueError("model-dir requires tokenizer files")
        if device != "cpu" and not (
            device == "cuda" or (device.startswith("cuda:") and device[5:].isdigit())
        ):
            raise ValueError("device must be cpu, cuda or cuda:N")
        # Must be set BEFORE lazy importing transformers/huggingface_hub.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                "Install optional dependencies: pip install -e '.[zeroshot]'"
            ) from exc
        self.model_path = str(model_dir)
        self._pipeline = pipeline(
            task="zero-shot-classification", model=self.model_path,
            tokenizer=self.model_path, device=device,
        )

    def classify(self, texts: list[str], hypotheses: list[str]) -> list[dict]:
        # In multi_label=True mode each label gets an independent
        # entailment-vs-contradiction score. The text is NEVER put in the
        # output report. Explicit batch_size bounds HF internal inference.
        result = self._pipeline(
            texts, candidate_labels=hypotheses, hypothesis_template="{}",
            multi_label=True, batch_size=8, truncation=True,
        )
        return [result] if isinstance(result, dict) else result


def _scores(raw: dict, hypotheses: dict[str, str]) -> dict[str, float]:
    """Strictly match the *returned* label strings to this dimension.

    Never assume the Hugging Face pipeline preserves candidate order.
    Reject partial, duplicate, extra, nonfinite or out-of-range outputs.
    """
    if not isinstance(raw, dict) or set(raw) < {"labels", "scores"}:
        raise ValueError("invalid prediction shape")
    labels, scores = raw["labels"], raw["scores"]
    if not isinstance(labels, list) or not isinstance(scores, list):
        raise ValueError("invalid prediction array types")
    if len(labels) != len(scores) or len(labels) != len(hypotheses):
        raise ValueError("missing prediction hypotheses")
    expected = {phrase: key for key, phrase in hypotheses.items()}
    if any(type(label) is not str for label in labels) or set(labels) != set(expected):
        raise ValueError("unexpected prediction hypotheses")
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate prediction hypotheses")
    if any(type(x) not in (float, int) or not math.isfinite(x) or not 0 <= x <= 1
           for x in scores):
        raise ValueError("invalid prediction scores")
    return {expected[label]: float(score) for label, score in zip(labels, scores)}


def _select(raw: dict, hypotheses: dict[str, str], *,
            min_score: float, min_margin: float) -> dict:
    scores = _scores(raw, hypotheses)
    ranked = sorted(scores, key=lambda label: (-scores[label], label))
    top, runner_up = ranked[:2]
    top_score = scores[top]
    margin = top_score - scores[runner_up]
    prediction = top if top_score >= min_score and margin >= min_margin else None
    reason = ("ACCEPTED" if prediction is not None else
              "BELOW_MIN_SCORE" if top_score < min_score else "BELOW_MIN_MARGIN")
    return {
        "status": "PREDICTED" if prediction is not None else "ABSTAINED",
        "reason": reason, "prediction": prediction, "top_label": top,
        "top_score": round(top_score, 6), "margin": round(margin, 6),
        "scores": {key: round(scores[key], 6) for key in hypotheses},
    }


def _error(exc: Exception) -> dict:
    # No exception string or raw model content: errors can contain source text.
    return {
        "status": "MODEL_ERROR", "reason": type(exc).__name__,
        "prediction": None, "top_label": None, "top_score": None,
        "margin": None, "scores": None,
    }


def _metrics(cases: list[dict], dimension: str, *, reference: str) -> dict:
    """Source labels are AUTHOR DRAFTS, not adjudicated ground truth."""
    rows = [row[reference][dimension] for row in cases]
    supported = sum(row["prediction"] is not None for row in rows)
    correct = sum(
        row["prediction"] is not None
        and row["prediction"] == case["draft"][dimension]
        for row, case in zip(rows, cases)
    )
    return {
        "supported": supported, "correct": correct,
        "incorrect": supported - correct,
        "abstained": len(cases) - supported,
        "errors": sum(row["status"] == "MODEL_ERROR" for row in rows),
        "conditional_agreement": round(correct / supported, 6) if supported else None,
        "coverage": round(supported / len(cases), 6) if cases else None,
    }


def evaluate(rows: list[dict], *, backend: ZeroShotBackend,
             batch_size: int = 4, min_score: float = 0.0,
             min_margin: float = 0.0) -> dict:
    """Independently score 4 bounded semantic dimensions on existing rows.

    Draft labels and groups are used *only* for scoring and are never
    passed to the classifier. NLI output cannot produce any Memory scope.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise ValueError("batch-size must be 1..32")
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1
           for x in (min_score, min_margin)):
        raise ValueError("thresholds must be finite floats in [0,1]")
    if not rows or len(rows) > 250:
        raise ValueError("expected 1..250 examples")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate example IDs")
    cases = []
    for row in rows:
        baseline = extract_scope(row["text"], row["id"])
        baseline_labels = baseline["labels"]
        cases.append({
            "id": row["id"], "group": row["group"],
            "draft": {key: row["draft"][key] for key in HYPOTHESES},
            "baseline": {
                key: {
                    "status": "PREDICTED" if baseline_labels is not None
                              and baseline_labels[key] is not None else "ABSTAINED",
                    "prediction": (
                        None if baseline_labels is None else baseline_labels[key]
                    ),
                } for key in HYPOTHESES
            },
            "zero_shot": {},
        })
    batches = []
    for dimension, hypotheses in HYPOTHESES.items():
        phrases = list(hypotheses.values())
        for start in range(0, len(rows), batch_size):
            subset = rows[start:start + batch_size]
            began = perf_counter()
            try:
                predictions = backend.classify([r["text"] for r in subset], phrases)
                if not isinstance(predictions, list) or len(predictions) != len(subset):
                    raise ValueError("model batch length mismatch")
                assessed = [
                    _select(result, hypotheses, min_score=min_score,
                            min_margin=min_margin)
                    for result in predictions
                ]
                status, reason = "OK", None
            except Exception as exc:
                assessed = [_error(exc) for _ in subset]
                status, reason = "ERROR", type(exc).__name__
            elapsed_ms = round((perf_counter() - began) * 1000, 3)
            batches.append({
                "dimension": dimension, "size": len(subset),
                "elapsed_ms": elapsed_ms, "status": status, "reason": reason,
            })
            for index, result in enumerate(assessed, start=start):
                cases[index]["zero_shot"][dimension] = result
    for case in cases:
        for dimension in HYPOTHESES:
            case["zero_shot"][dimension]["matches_draft"] = (
                case["zero_shot"][dimension]["prediction"] is not None
                and case["zero_shot"][dimension]["prediction"]
                == case["draft"][dimension]
            )
    by_dimension = {
        key: {
            "baseline": _metrics(cases, key, reference="baseline"),
            "zero_shot": _metrics(cases, key, reference="zero_shot"),
        }
        for key in HYPOTHESES
    }
    by_group = {
        name: {
            key: {
                "zero_shot": _metrics(subset, key, reference="zero_shot"),
                "baseline": _metrics(subset, key, reference="baseline"),
            } for key in HYPOTHESES
        }
        for name in sorted({r["group"] for r in cases})
        for subset in ([r for r in cases if r["group"] == name],)
    }
    durations = [b["elapsed_ms"] for b in batches]
    return {
        "total": len(cases), "dimensions": list(HYPOTHESES),
        "batch_calls": len(batches),
        "error_batches": sum(b["status"] == "ERROR" for b in batches),
        "total_inference_ms": round(sum(durations), 3),
        "median_batch_ms": median(durations),
        "approx_sentences_per_second": (
            round(len(cases) * len(HYPOTHESES) / (sum(durations) / 1000), 3)
            if sum(durations) > 0 else None
        ),
        "by_dimension": by_dimension, "by_group": by_group,
        "batches": batches, "cases": cases,
    }


def _write_once(path: Path, data: dict):
    if path.exists():
        raise ValueError("refusing to overwrite existing report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        json.dump(data, out, indent=2, ensure_ascii=False, allow_nan=False)
        out.write("\n")


def main(argv=None, *, backend: ZeroShotBackend | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c13_semantic_scope_corpus.json"))
    parser.add_argument("--model-dir", type=Path, required=True,
                        help="Existing LOCAL Transformers DeBERTa model directory")
    parser.add_argument("--device", default="cpu", help="cpu, cuda, or cuda:N")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--min-score", type=float, default=0.0,
                        help="Uncalibrated score threshold (default: no abstention)")
    parser.add_argument("--min-margin", type=float, default=0.0,
                        help="Minimum top-vs-runner-up margin (default: none)")
    parser.add_argument("--case-ids", nargs="+",
                        help="Optional smoke-test subset (IDs must exist)")
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite existing report")
    try:
        rows, digest = load_corpus(args.suite)
        if args.case_ids:
            chosen = set(args.case_ids)
            if len(chosen) != len(args.case_ids) or not chosen.issubset(
                {row["id"] for row in rows}
            ):
                raise ValueError("duplicate or unknown case IDs")
            rows = [row for row in rows if row["id"] in chosen]
        start = perf_counter()
        adapter = backend or LocalTransformersZeroShot(
            args.model_dir, device=args.device,
        )
        load_ms = round((perf_counter() - start) * 1000, 3)
        results = evaluate(
            rows, backend=adapter, batch_size=args.batch_size,
            min_score=args.min_score, min_margin=args.min_margin,
        )
        report = {
            "version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_version": DATASET_VERSION,
            "dataset_sha256": digest,
            "annotation_status": LABEL_STATUS,
            "hypotheses_version": HYPOTHESES_VERSION,
            "hypotheses_sha256": sha256(
                json.dumps(HYPOTHESES, sort_keys=True).encode()
            ).hexdigest(),
            "model_dir": args.model_dir.expanduser().name,
            "device": args.device,
            "batch_size": args.batch_size,
            "min_score": args.min_score, "min_margin": args.min_margin,
            "full_corpus": not bool(args.case_ids),
            "model_load_ms": load_ms,
            "metrics": results,
            "limits": [
                "Synthetic draft labels are not independently adjudicated.",
                "Zero-shot NLI entailment-vs-contradiction scores are not calibrated.",
                "Top-1 per dimension is a diagnostic; compound claims may have multiple true labels.",
                "UNKNOWN/UNSPECIFIED are explicit hypotheses, not calibrated abstention.",
                "Thresholds are experimental, not safety-certified.",
                "NLI dimensions do not establish subjects, predicates, evidence spans, or Memory scope.",
                "No user text or raw model output is persisted to the report.",
                "No Memory writes, PostgreSQL access, Ollama calls or online downloads.",
                "Do not treat the development corpus as an untouched holdout.",
            ],
        }
        _write_once(args.report, report)
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Zero-shot evaluation failed: {type(exc).__name__}: {exc}\n")
    print(f"Evaluated {len(rows)} cases on four dimensions "
          f"({results['batch_calls']} batches, "
          f"{results['error_batches']} failed); saved {args.report}")
    return 1 if results["error_batches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
