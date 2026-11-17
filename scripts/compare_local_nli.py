#!/usr/bin/env python3
"""Compare locally cached DeBERTa-v3 base/large NLI ONNX models on matched pairs.

Does not download models, call Ollama, change persona state or persist outputs.
Run against both FP32 and CPU INT8 to diagnose the prior neutral->contradiction
error; synthetic results are not production reliability estimates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

from persona_agent.local_onnx import (
    LocalONNXConfig, LocalONNXModel, cpu_instruction_flags, select_onnx_file,
)
from persona_agent.nli_evaluation import (
    NLIPair, compare_nli_runs, load_nli_suite, summarize_nli,
)

MODEL_NAMES = ("nli-deberta-v3-base", "nli-deberta-v3-large")


def _percentile(values: list[float], fraction: float) -> float:
    sorted_values = sorted(values)
    return round(sorted_values[min(len(values)-1, int((len(values)-1)*fraction+0.999999))], 3)


def _sha256_if_present(path: Path) -> str | None:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def input_diagnostics(model: LocalONNXModel, pair: NLIPair) -> dict:
    """Expose graph/label/pair encoding metadata, not guessed NLI output order."""
    tokenizer = model.tokenizer
    inputs = tokenizer(
        [pair.premise], [pair.hypothesis],
        padding=True, truncation=True, max_length=256, return_tensors="np",
    )
    tokens = inputs["input_ids"][0]
    sep_id = tokenizer.sep_token_id
    return {
        "tokenizer_class":type(tokenizer).__name__,
        "pair_example_id":pair.id,
        "pair_tokens":len(tokens),
        "separator_token_id":sep_id,
        "separator_count":int((tokens == sep_id).sum()) if sep_id is not None else None,
        "onnx_inputs":[{"name":x.name,"type":x.type}
                       for x in model.session.get_inputs()],
        "nli_label_indices":dict(model.labels),
        "max_length":256,
        "note":"Check that both texts appear as a pair. This is not a PyTorch/ONNX parity test.",
    }


def evaluate_variant(
    examples: list[NLIPair], *,
    model_dir: Path, precision: str, threads: int,
    repetitions: int, warmup: int,
    model_factory=LocalONNXModel,
) -> dict:
    started=perf_counter()
    model=model_factory(LocalONNXConfig(
        model_dir=model_dir,role="nli",precision=precision,
        intra_op_threads=threads,
    ))
    load_ms=round((perf_counter()-started)*1000,3)
    for _ in range(warmup):
        model.nli(examples[0].premise,examples[0].hypothesis)
    timing_ms=[]
    suite_ms=[]
    outputs={}
    for _ in range(repetitions):
        suite_start=perf_counter()
        for case in examples:
            start=perf_counter()
            probabilities=model.nli(case.premise,case.hypothesis)
            timing_ms.append((perf_counter()-start)*1000)
            outputs[case.id]=probabilities
        suite_ms.append((perf_counter()-suite_start)*1000)
    metrics=summarize_nli(examples,outputs)
    config_path=model_dir/"config.json"
    model_file=Path(model.model_file)
    return {
        "key":f"{model_dir.name}:{precision}",
        "model":model_dir.name,
        "precision":precision,
        "resolved_onnx_file":str(model_file),
        "onnx_file_size_bytes":model_file.stat().st_size,
        "model_config_sha256":_sha256_if_present(config_path),
        "onnxruntime_version":model.ort_version,
        "execution_provider":"CPUExecutionProvider",
        "load_ms":load_ms,
        "mean_pair_latency_ms":round(statistics.mean(timing_ms),3),
        "median_pair_latency_ms":round(statistics.median(timing_ms),3),
        "p95_pair_latency_ms":_percentile(timing_ms,0.95),
        "median_full_suite_ms":round(statistics.median(suite_ms),3),
        "repetitions":repetitions,
        "pair_count":len(examples),
        "input_diagnostics":input_diagnostics(model,examples[0]),
        "metrics":metrics,
    }


def run(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models-root",type=Path,
        default=Path(os.environ.get("PERSONA_MODELS_DIR","~/models")).expanduser(),
    )
    parser.add_argument("--suite",type=Path,
                        default=Path("scenarios/mvp02c5_nli_comparison.json"))
    parser.add_argument("--models",nargs="+",choices=MODEL_NAMES,
                        default=list(MODEL_NAMES))
    parser.add_argument("--precisions",nargs="+",choices=("fp32","int8"),
                        default=["fp32","int8"])
    parser.add_argument("--intra-op-threads",type=int,default=2)
    parser.add_argument("--repetitions",type=int,default=2)
    parser.add_argument("--warmup",type=int,default=1,
                        help="Warm up using the first pair per model session")
    parser.add_argument("--report",type=Path,required=True,
                        help="Create a NEW JSON report (never overwrite)")
    args=parser.parse_args(argv)
    if not 1 <= args.intra_op_threads <= 32:
        parser.error("intra-op-threads must be 1..32")
    if not 1 <= args.repetitions <= 20 or not 0 <= args.warmup <= 10:
        parser.error("repetitions must be 1..20, warmup must be 0..10")
    if len(set(args.models)) != len(args.models) or len(set(args.precisions)) != len(args.precisions):
        parser.error("duplicate models or precisions not permitted")
    if args.report.exists():
        parser.error("refusing to overwrite an existing report")
    try:
        examples,dataset_hash=load_nli_suite(args.suite)
        root=args.models_root.expanduser().resolve()
        # Preflight every variant before loading potentially multi-GB models.
        for name in args.models:
            for precision in args.precisions:
                select_onnx_file(root/name,precision=precision)
            if not (root/name/"config.json").is_file():
                raise FileNotFoundError(f"Missing local config.json: {root/name}")
        runs=[]
        for name in args.models:
            for precision in args.precisions:
                run_info=evaluate_variant(
                    examples,model_dir=root/name,precision=precision,
                    threads=args.intra_op_threads,
                    repetitions=args.repetitions,warmup=args.warmup,
                )
                runs.append(run_info)
                print(
                    f"{run_info['key']}: {run_info['metrics']['correct']}/"
                    f"{run_info['metrics']['total']} correct; "
                    f"false contradictions={run_info['metrics']['false_contradiction_count']}; "
                    f"p95 pair={run_info['p95_pair_latency_ms']} ms",
                    file=sys.stderr,
                )
        report={
            "format_version":"mvp02c5.nli_comparison.1",
            "created_at":datetime.now(timezone.utc).isoformat(),
            "suite":{"path":str(args.suite),"sha256":dataset_hash,
                     "examples":len(examples),"provenance":"curated synthetic, not representative"},
            "host":{"machine":platform.machine(),"python":platform.python_version(),
                    "cpu_flags":sorted(cpu_instruction_flags() & frozenset({
                        "avx2","avx512f","avx512_vnni","avx_vnni",
                    }))},
            "settings":{"threads":args.intra_op_threads,
                        "repetitions":args.repetitions,"warmup":args.warmup},
            "runs":runs,
            "comparisons":compare_nli_runs(runs),
            "caveats":[
                "Scores are uncalibrated model softmax, NOT probability that a memory is false.",
                "Hand-labeled synthetic examples are not representative of production conversations.",
                "No PyTorch reference inference or ONNX parity check was performed.",
                "Sequential runs share a process; timing is not concurrent/Ollama-loaded performance.",
                "NLI contradiction is advisory only, not authorization to update a memory.",
            ],
        }
        args.report.parent.mkdir(parents=True,exist_ok=True)
        with args.report.open("x",encoding="utf-8") as target:
            json.dump(report,target,indent=2)
            target.write("\n")
        print(f"Saved report: {args.report}")
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2,f"NLI comparison failed: {type(exc).__name__}: {exc}\n")


if __name__=="__main__":
    raise SystemExit(run())
