#!/usr/bin/env python3
"""Offline CPU NLI comparison on a new draft diagnostic suite.

No model download or persona state mutation. Custom checkpoints require
explicit review and opt-in. This is not a production memory conflict policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
from time import perf_counter

# Set before importing the ML libraries; existing local checkpoints only.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from persona_agent.nli_evaluation import (
    compare_nli_runs, load_nli_suite, summarize_nli,
)

MODEL_NAMES = (
    "nli-deberta-v3-base",
    "nli-deberta-v3-large",
    "mDeBERTa-v3-base-xnli-multilingual-nli-2mil7",
    "finecat-nli-v2-xl",
    "AbsoluteZero-nli-4b",
)
CUSTOM_CODE = frozenset({"finecat-nli-v2-xl", "AbsoluteZero-nli-4b"})
LABELS = frozenset({"entailment", "neutral", "contradiction"})
CONTRADICTION_THRESHOLD = 0.90  # Predeclared diagnostic cutoff; never a Memory permission.


def label_indices(config) -> dict[str, int]:
    raw = getattr(config, "id2label", None)
    if not isinstance(raw, dict) or len(raw) != 3:
        raise ValueError("NLI config must specify three label indices")
    mapping = {str(label).strip().casefold(): int(index) for index, label in raw.items()}
    if set(mapping) != LABELS or set(mapping.values()) != {0, 1, 2}:
        raise ValueError(f"Unsupported label mapping: {raw!r}")
    return mapping


def _predictor(model_dir: Path, name: str, allow_custom: bool):
    if name in CUSTOM_CODE:
        if not allow_custom:
            raise ValueError(f"{name} requires reviewed local custom code and --allow-reviewed-custom-code")
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(str(model_dir), device="cpu", trust_remote_code=True, max_length=256)
        mapping = label_indices(model.model.config)

        def predict(premise: str, hypothesis: str) -> dict[str, float]:
            import numpy as np
            scores = np.asarray(model.predict(
                [(premise, hypothesis)], batch_size=1, apply_softmax=True,
                show_progress_bar=False,
            ))
            if scores.shape != (1, 3):
                raise ValueError(f"Unexpected NLI scores: {scores.shape}")
            return {key: float(scores[0, index]) for key, index in mapping.items()}

        return mapping, predict, "sentence-transformers/CrossEncoder"

    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    import torch
    tokenizer = AutoTokenizer.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False,
        use_fast=True,
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False,
    ).to("cpu").eval()
    mapping = label_indices(model.config)

    def predict(premise: str, hypothesis: str) -> dict[str, float]:
        inputs = tokenizer(
            premise, hypothesis, truncation=True, max_length=256,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = model(**inputs).logits
            if tuple(logits.shape) != (1, 3):
                raise ValueError(f"Unexpected NLI logits: {tuple(logits.shape)}")
            scores = torch.softmax(logits[0].float(), -1).cpu().tolist()
        return {key: float(scores[index]) for key, index in mapping.items()}

    return mapping, predict, "transformers/AutoModelForSequenceClassification"


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .95 + .999999))], 3)


def benchmark(model_dir: Path, name: str, cases, *, warmup: int,
              repetitions: int, allow_custom: bool) -> dict:
    if not (model_dir / "config.json").is_file() or not (model_dir / "model.safetensors").is_file():
        raise FileNotFoundError(f"Missing local model files in {model_dir}")
    start = perf_counter()
    mapping, predict, backend = _predictor(model_dir, name, allow_custom)
    loading_ms = round((perf_counter() - start) * 1000, 3)
    try:
        for _ in range(warmup):
            predict(cases[0].premise, cases[0].hypothesis)
        timings = []
        suite_timings = []
        outputs = {}
        for _ in range(repetitions):
            suite_start = perf_counter()
            for case in cases:
                pair_start = perf_counter()
                outputs[case.id] = predict(case.premise, case.hypothesis)
                timings.append((perf_counter() - pair_start) * 1000)
            suite_timings.append((perf_counter() - suite_start) * 1000)
        scores = summarize_nli(
            cases, outputs, high_confidence_threshold=CONTRADICTION_THRESHOLD,
        )
        config_bytes = (model_dir / "config.json").read_bytes()
        return {
            "key": name + ":torch-native", "model": name, "backend": backend,
            "device": "cpu", "nli_label_indices": mapping,
            "model_config_sha256": sha256(config_bytes).hexdigest(),
            "checkpoint_size_bytes": (model_dir / "model.safetensors").stat().st_size,
            "load_ms": loading_ms,
            "mean_pair_latency_ms": round(statistics.mean(timings), 3),
            "median_pair_latency_ms": round(statistics.median(timings), 3),
            "p95_pair_latency_ms": _p95(timings),
            "median_full_suite_ms": round(statistics.median(suite_timings), 3),
            "pair_count": len(cases), "repetitions": repetitions, "metrics": scores,
        }
    finally:
        del predict
        gc.collect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c7_nli_threshold_diagnostics.json"),
        help="Path to the reviewed or draft NLI diagnostic suite")
    parser.add_argument("--models-root", type=Path,
                        default=Path("~/models/nli").expanduser())
    parser.add_argument("--models", nargs="+", choices=MODEL_NAMES,
                        default=["mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"])
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--allow-reviewed-custom-code", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.threads <= 32 or not 0 <= args.warmup <= 10 or not 1 <= args.repetitions <= 20:
        parser.error("invalid threads/warmup/repetitions")
    if len(args.models) != len(set(args.models)):
        parser.error("duplicate model selection")
    if args.report.exists():
        parser.error("refusing to overwrite an existing report")
    if any(name in CUSTOM_CODE for name in args.models) and not args.allow_reviewed_custom_code:
        parser.error("custom checkpoints require --allow-reviewed-custom-code")
    cases, suite_hash = load_nli_suite(args.suite)
    provenance = json.loads(args.suite.read_text(encoding="utf-8")).get("provenance")
    import torch
    torch.set_num_threads(args.threads)
    runs, failures = [], []
    for name in args.models:
        print(f"Evaluating {name} ...", flush=True)
        try:
            run = benchmark(args.models_root.expanduser() / name, name, cases,
                            warmup=args.warmup, repetitions=args.repetitions,
                            allow_custom=args.allow_reviewed_custom_code)
            runs.append(run)
            cut = run["metrics"]["contradiction_threshold"]
            print(f"  correct={run['metrics']['correct']}/{len(cases)} "
                  f"threshold_TP={cut['true_positive']} threshold_FP={cut['false_positive']} "
                  f"threshold_FN={cut['false_negative']}", flush=True)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print("  FAILED: " + failures[-1], flush=True)
        finally:
            gc.collect()
    report = {
        "format_version": "mvp02c7.nli_threshold.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": {"path": str(args.suite), "sha256": suite_hash,
                  "examples": len(cases), "provenance": provenance},
        "settings": {"threads": args.threads, "warmup": args.warmup,
                     "repetitions": args.repetitions,
                     "threshold": CONTRADICTION_THRESHOLD},
        "host": {"torch_version": torch.__version__, "device": "cpu"},
        "runs": runs, "failures": failures,
        "comparisons": compare_nli_runs(runs) if len(runs) >= 2 else [],
        "caveats": [
            "Draft labels need independent human review; not a blinded or representative holdout.",
            "0.90 is a fixed diagnostic cutoff, not a calibrated fact or memory confidence.",
            "Scores are advisory; domain-owned commands alone may change persona memory.",
            "Native PyTorch timings cannot be compared directly with ONNX timings.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as target:
        json.dump(report, target, indent=2, ensure_ascii=False)
        target.write("\n")
    print(f"Saved {args.report}; {len(runs)} successful, {len(failures)} failures")
    return 0 if runs and not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
