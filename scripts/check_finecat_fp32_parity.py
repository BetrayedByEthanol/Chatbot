#!/usr/bin/env python3
"""Offline FP32 PyTorch/ONNX parity regression for reviewed local FineCat.

Use the *export-compatible* Transformers 4.57.6 environment. No model
fetching, exports, quantization, or persona state modifications occur.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path

# These must be set before loading any model libraries.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

REQUIRED_TRANSFORMERS = "4.57.6"
FOCUSED_IDS = ("N02", "N08", "HN026", "HE011", "HC010")
SUITES = (
    Path("scenarios/mvp02c5_nli_comparison.json"),
    Path("scenarios/mvp02c7_nli_threshold_diagnostics.json"),
)
LOGIT_TOL = 1e-4
PROB_TOL = 1e-5
THRESHOLD = 0.90  # Diagnostic only, never Memory authority.


def require_transformers_version(actual: str) -> None:
    """The known-good export baseline is intentionally strict."""
    if actual != REQUIRED_TRANSFORMERS:
        raise ValueError(
            f"FineCat export parity requires transformers=={REQUIRED_TRANSFORMERS}; "
            f"found {actual}. Use the original export environment."
        )


def label_indices(id2label: dict) -> dict[str, int]:
    result = {str(label).strip().casefold(): int(index)
              for index, label in id2label.items()}
    if result != {"entailment": 0, "neutral": 1, "contradiction": 2}:
        raise ValueError(f"Unexpected FineCat label mapping: {id2label!r}")
    return result


def probabilities(logits: list[float]) -> list[float]:
    if len(logits) != 3 or not all(math.isfinite(v) for v in logits):
        raise ValueError("Expected exactly three finite NLI logits")
    center = max(logits)
    values = [math.exp(v - center) for v in logits]
    total = sum(values)
    return [v / total for v in values]


def compare_logits(native: list[float], onnx: list[float]) -> dict:
    pn, po = probabilities(native), probabilities(onnx)
    logit_delta = max(abs(a - b) for a, b in zip(native, onnx))
    probability_delta = max(abs(a - b) for a, b in zip(pn, po))
    argmax_equal = pn.index(max(pn)) == po.index(max(po))
    threshold_equal = (pn[2] >= THRESHOLD) == (po[2] >= THRESHOLD)
    return {
        "max_logit_delta": logit_delta,
        "max_probability_delta": probability_delta,
        "argmax_equal": argmax_equal,
        "threshold_equal": threshold_equal,
        "passed": (logit_delta <= LOGIT_TOL and probability_delta <= PROB_TOL
                   and argmax_equal and threshold_equal),
    }


def select_cases(all_cases: bool):
    from persona_agent.nli_evaluation import load_nli_suite

    cases, seen, sources = [], set(), []
    for path in SUITES:
        suite, digest = load_nli_suite(path)
        sources.append({"path": str(path), "sha256": digest, "count": len(suite)})
        for case in suite:
            if case.id in seen:
                raise ValueError(f"Duplicated case ID {case.id}")
            seen.add(case.id)
            cases.append(case)
    if len(cases) != 174:
        raise ValueError(f"Expected 174 fixed diagnostic pairs, got {len(cases)}")
    if not all_cases:
        selected = {case.id: case for case in cases}
        if any(key not in selected for key in FOCUSED_IDS):
            raise ValueError("Missing focused regression case")
        cases = [selected[key] for key in FOCUSED_IDS]
    return cases, sources


def parity(model_dir: Path, cases: list, threads: int) -> tuple[list[dict], str]:
    import numpy as np
    import onnxruntime as ort
    import torch
    import transformers
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    require_transformers_version(transformers.__version__)
    torch.set_num_threads(threads)
    tokenizer = AutoTokenizer.from_pretrained(
        str(model_dir), use_fast=True, trust_remote_code=False,
        local_files_only=True,
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), trust_remote_code=True, local_files_only=True,
    ).to("cpu").eval()
    label_indices(model.config.id2label)
    graph = model_dir / "onnx" / "model.onnx"
    if not graph.is_file():
        raise FileNotFoundError(f"Missing exported ONNX FP32 graph: {graph}")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    session = ort.InferenceSession(
        str(graph), sess_options=opts, providers=["CPUExecutionProvider"],
    )
    if "CPUExecutionProvider" not in session.get_providers():
        raise RuntimeError("ONNX CPU provider unavailable")

    results = []
    with torch.inference_mode():
        for case in cases:
            inputs = tokenizer(
                [case.premise], [case.hypothesis], truncation=True,
                padding=True, max_length=256, return_tensors="pt",
            )
            native = model(**inputs).logits.detach().float().cpu().numpy()
            feeds = {}
            for node in session.get_inputs():
                if node.name not in inputs:
                    raise ValueError(f"Tokenizer did not supply ONNX input {node.name!r}")
                if node.type not in ("tensor(int64)", "tensor(int32)"):
                    raise ValueError(f"Unexpected ONNX input dtype {node.type!r}")
                dtype = np.int64 if node.type == "tensor(int64)" else np.int32
                feeds[node.name] = inputs[node.name].detach().cpu().numpy().astype(
                    dtype, copy=False,
                )
            onnx = np.asarray(session.run(None, feeds)[0])
            if native.shape != (1, 3) or onnx.shape != (1, 3):
                raise ValueError(f"Invalid NLI logits shapes: {native.shape}, {onnx.shape}")
            result = compare_logits(native[0].tolist(), onnx[0].tolist())
            results.append({"id": case.id, **result})
    return results, ort.__version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path(
        "~/models/nli/finecat-nli-v2-xl"))
    parser.add_argument("--all", action="store_true", help="Check all 174 cases (default: five regressions)")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--allow-reviewed-custom-code", action="store_true")
    parser.add_argument("--report", type=Path, required=True,
                        help="Create a new JSON report; never overwrite")
    args = parser.parse_args(argv)
    if not args.allow_reviewed_custom_code:
        parser.error("review local FineCat custom code before --allow-reviewed-custom-code")
    if not 1 <= args.threads <= 32:
        parser.error("--threads must be 1..32")
    model_dir = args.model_dir.expanduser().resolve()
    if model_dir.name != "finecat-nli-v2-xl":
        parser.error("Only finecat-nli-v2-xl is supported")
    if not (model_dir / "config.json").is_file():
        parser.error("Missing locally cached config.json")
    if not (model_dir / "onnx" / "model.onnx").is_file():
        parser.error("Missing locally exported ONNX FP32 graph")
    path = args.report.expanduser().resolve()
    if path.exists():
        parser.error("refusing to overwrite an existing report")

    # Fail before loading a multi-gigabyte model on known-incompatible versions.
    from importlib.metadata import PackageNotFoundError, version
    try:
        require_transformers_version(version("transformers"))
    except (ValueError, PackageNotFoundError) as exc:
        parser.error(str(exc))
    cases, sources = select_cases(args.all)
    measurements, ort_version = parity(model_dir, cases, args.threads)
    passed = all(row["passed"] for row in measurements)
    report = {
        "format_version": "mvp02c9.fp32_parity.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model_dir.name,
        "config_sha256": sha256((model_dir / "config.json").read_bytes()).hexdigest(),
        "suite_sources": sources,
        "runtime": {"transformers": REQUIRED_TRANSFORMERS,
                    "onnxruntime": ort_version, "threads": args.threads},
        "limits": {"max_logit_delta": LOGIT_TOL,
                   "max_probability_delta": PROB_TOL,
                   "contradiction_threshold": THRESHOLD},
        "checked": len(measurements),
        "passed": passed,
        "max_logit_delta": max(x["max_logit_delta"] for x in measurements),
        "max_probability_delta": max(x["max_probability_delta"] for x in measurements),
        "argmax_changes": sum(not x["argmax_equal"] for x in measurements),
        "threshold_changes": sum(not x["threshold_equal"] for x in measurements),
        "cases": measurements,
        "limitations": "Parity to the existing ONNX export; not an INT8 validation or memory-admission policy.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print(f"Checked {len(measurements)}; passed={passed}; "
          f"max_logit_delta={report['max_logit_delta']:.9g}; "
          f"max_probability_delta={report['max_probability_delta']:.9g}; "
          f"argmax_changes={report['argmax_changes']}; "
          f"threshold_changes={report['threshold_changes']}; report={path}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
