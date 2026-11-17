#!/usr/bin/env python3
"""Offline CPU ONNX smoke benchmark; intentionally independent of Ollama/DB.

Examples:
  python scripts/benchmark_local_onnx.py --models-root ~/models --role all
  python scripts/benchmark_local_onnx.py --models-root ~/models --role nli --precision fp32
  python scripts/benchmark_local_onnx.py --models-root ~/models --role all \\
      --precision int8 --report artifacts/onnx-threadripper-int8.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

from persona_agent.local_onnx import (
    LocalONNXConfig, LocalONNXModel, cpu_instruction_flags,
)

NLI_EXAMPLES = (
    ("I enjoy gardening.", "I enjoy gardening.", "entailment"),
    ("I enjoy gardening.", "I do not enjoy gardening.", "contradiction"),
    ("I enjoy gardening.", "I work as a programmer.", "neutral"),
)
EMBEDDING_EXAMPLES = (
    ("I enjoy gardening.", "Gardening is a hobby I like.", "I repaired my computer yesterday."),
    ("I like taking walks.", "I enjoy going for a walk.", "The database is running."),
    ("I read science fiction.", "Science fiction books interest me.", "I baked a loaf of bread."),
)

MODEL_DIRECTORIES = {
    "embedding": "all-MiniLM-L6-v2",
    "nli": "nli-deberta-v3-base",
}


def _pct(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered)-1, int((len(ordered)-1)*fraction+0.999999)))
    return round(ordered[index], 3)


def _max_rss_mb() -> float | None:
    try:
        import resource
        # Linux reports KB; macOS reports bytes. This benchmark targets Linux.
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(raw / (1024 if sys.platform.startswith("linux") else 1048576), 2)
    except (ImportError, OSError):
        return None


def benchmark_one(
    role: str, *, models_root: Path, precision: str, threads: int,
    repetitions: int, warmup: int, nli_label_order=None,
    model_factory=LocalONNXModel,
) -> dict:
    model_dir=models_root/MODEL_DIRECTORIES[role]
    start=perf_counter()
    model=model_factory(LocalONNXConfig(
        model_dir=model_dir,role=role,precision=precision,
        intra_op_threads=threads,
        explicit_nli_label_order=nli_label_order if role=="nli" else None,
    ))
    load_ms=round((perf_counter()-start)*1000,3)

    def classify():
        if role=="nli":
            return [
                model.nli(premise,hypothesis)
                for premise,hypothesis,_ in NLI_EXAMPLES
            ]
        output=[]
        for anchor,positive,negative in EMBEDDING_EXAMPLES:
            vectors=model.embed([anchor,positive,negative])
            output.append({
                "positive_cosine":float(vectors[0] @ vectors[1]),
                "negative_cosine":float(vectors[0] @ vectors[2]),
            })
        return output

    for _ in range(warmup):
        classify()
    times=[]
    predictions=None
    for _ in range(repetitions):
        begin=perf_counter()
        predictions=classify()
        times.append((perf_counter()-begin)*1000)
    if role=="nli":
        cases=[{
            "expected":gold,
            "predicted":max(probabilities,key=probabilities.get),
            "scores":{key:round(value,6) for key,value in probabilities.items()},
        } for probabilities,(_,_,gold) in zip(predictions,NLI_EXAMPLES)]
        correct=sum(c["expected"]==c["predicted"] for c in cases)
    else:
        cases=[{
            "positive_cosine":round(x["positive_cosine"],6),
            "negative_cosine":round(x["negative_cosine"],6),
            "positive_above_negative":x["positive_cosine"]>x["negative_cosine"],
        } for x in predictions]
        correct=sum(c["positive_above_negative"] for c in cases)
    return {
        "role":role,
        "directory":str(model_dir),
        "model_file":str(model.model_file),
        "model_bytes":model.model_file.stat().st_size,
        "onnxruntime_version":model.ort_version,
        "provider":"CPUExecutionProvider",
        "load_ms":load_ms,
        "median_benchmark_ms":round(statistics.median(times),3),
        "p95_benchmark_ms":_pct(times,0.95),
        "observations":repetitions,
        "examples_per_observation":len(cases),
        "process_peak_rss_mb":_max_rss_mb(),
        "smoke_correct":correct,
        "smoke_total":len(cases),
        "smoke_cases":cases,
        "note":"Tiny synthetic smoke set; not a calibrated accuracy/precision evaluation.",
    }


def run(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models-root",type=Path,
        default=Path(os.environ.get("PERSONA_MODELS_DIR","~/models")).expanduser(),
        help="Directory with locally downloaded Hugging Face checkpoints",
    )
    parser.add_argument("--role",choices=("nli","embedding","all"),default="all")
    parser.add_argument("--precision",choices=("auto","fp32","int8"),default="auto")
    parser.add_argument("--intra-op-threads",type=int,default=2)
    parser.add_argument("--warmup",type=int,default=1)
    parser.add_argument("--repetitions",type=int,default=5)
    parser.add_argument(
        "--nli-label-order",nargs=3,
        metavar=("LABEL_0","LABEL_1","LABEL_2"),
        help="Verified index order only if local id2label is ambiguous",
    )
    parser.add_argument("--report",type=Path,
                        help="Write a new JSON report (never overwrite)")
    args=parser.parse_args(argv)
    if not 1 <= args.repetitions <= 100 or not 0 <= args.warmup <= 20:
        parser.error("repetitions must be 1..100 and warmup must be 0..20")
    if not 1 <= args.intra_op_threads <= 32:
        parser.error("intra-op-threads must be 1..32")
    if args.report and args.report.exists():
        parser.error("refusing to overwrite an existing report")
    if args.nli_label_order and args.role=="embedding":
        parser.error("--nli-label-order requires --role nli or all")

    roles=("embedding","nli") if args.role=="all" else (args.role,)
    result={
        "format_version":"mvp02c4.local_onnx.1",
        "created_at":datetime.now(timezone.utc).isoformat(),
        "host": {
            "machine":platform.machine(),
            "processor":platform.processor(),
            "cpu_flags":sorted(cpu_instruction_flags() & frozenset({
                "avx2","avx512f","avx512_vnni","avx_vnni",
            })),
            "python":platform.python_version(),
        },
        "settings":{
            "requested_precision":args.precision,
            "intra_op_threads":args.intra_op_threads,
            "warmup":args.warmup,
            "repetitions":args.repetitions,
        },
        "results":[],
    }
    try:
        for role in roles:
            result["results"].append(benchmark_one(
                role,models_root=args.models_root.expanduser(),
                precision=args.precision,threads=args.intra_op_threads,
                repetitions=args.repetitions,warmup=args.warmup,
                nli_label_order=tuple(args.nli_label_order)
                if args.nli_label_order else None,
            ))
    except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
        parser.exit(2,f"Local ONNX benchmark error: {type(exc).__name__}: {exc}\n")
    serialized=json.dumps(result,indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation: no partial overwrite of a prior report.
        with args.report.open("x",encoding="utf-8") as target:
            target.write(serialized+"\n")
        print(f"Saved local ONNX benchmark: {args.report}")
    else:
        print(serialized)
    return 0


if __name__=="__main__":
    raise SystemExit(run())
