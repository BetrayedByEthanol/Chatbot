#!/usr/bin/env python3
"""MVP-02C.14F: run actual local semantic backends with warmup and repetitions.

This is read-only, reference-claim-span-conditioned draft-label agreement,
NOT a production eligibility metric or an independently reviewed holdout.
Never writes source text, predictions, model completions, or exception messages.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
from statistics import median
from time import perf_counter
from urllib.request import Request

from evaluate_semantic_models import (
    DebertaNliProbe, OllamaJsonProbe, ProbeLabels,
    SpacyStructuralProbe, evaluate_probe,
)

VERSION = "mvp02c14f.real_semantic_benchmark.v1"


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return round(ordered[math.ceil((len(ordered) - 1) * ratio)], 3)


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _model_provenance(*, backend_name: str, backend,
                      model_dir: Path | None,
                      model_name: str | None) -> dict:
    if backend_name == "deberta":
        assert model_dir is not None
        folder = model_dir.expanduser().resolve(strict=True)
        config = folder / "config.json"
        if not config.is_file():
            raise ValueError("missing local HF config")
        weights = [folder / "model.safetensors", folder / "pytorch_model.bin"]
        selected = next((p for p in weights if p.is_file()), None)
        if selected is None:
            raise ValueError("missing local native model weights")
        # Content hash avoids aliasing distinct checkpoints under the same tag.
        digest = sha256()
        with selected.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return {
            "model_name": folder.name, "model_config_sha256":
                sha256(config.read_bytes()).hexdigest(),
            "weight_format": selected.suffix,
            "weights_size_bytes": selected.stat().st_size,
            "weights_sha256": digest.hexdigest(),
            "weights_local_only": True,
        }
    if backend_name == "ollama":
        # Capture the resolved local model digest, not just a mutable tag.
        # Use the same proxy-free loopback client as the inference backend.
        request = Request(backend.url + "/api/tags")
        with backend._opener.open(request, timeout=10) as response:
            blob = response.read(262145)
        if len(blob) > 262144:
            raise ValueError("oversized local Ollama tags response")
        data = json.loads(blob)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("invalid local Ollama model metadata")
        models = [item for item in data["models"]
                  if isinstance(item, dict)
                  and (item.get("name") == model_name
                       or item.get("model") == model_name)]
        if len(models) != 1:
            raise ValueError("configured model not present on local Ollama")
        model = models[0]
        digest = model.get("digest")
        if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ValueError("missing immutable Ollama model digest")
        return {
            "model_name": model_name, "model_tag_is_mutable": True,
            "digest_verified": True, "digest": digest,
            "size_bytes": model.get("size"),
        }
    return {"model_name": "spacy.blank_en", "trained_pipeline": False,
            "probe_revision": "spacy.blank_en.lexical_cues.v1"}


def _decision_thresholds(*, backend_name: str, backend) -> dict | None:
    """Read active NLI abstention cutoffs from the instantiated backend.

    CLI defaults or arguments are not authoritative if an injected backend
    overrides them. Other probes have no NLI score/margin cutoff.
    """
    if backend_name != "deberta":
        return None
    values = {}
    for key in ("min_score", "min_margin"):
        value = getattr(backend, key, None)
        if (type(value) not in (float, int) or not math.isfinite(value)
                or not 0 <= value <= 1):
            raise ValueError("DeBERTa backend must expose valid NLI thresholds")
        values[key] = float(value)
    return values


def _host() -> dict:
    return {
        "os": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "python_packages": {
            name: _package_version(name)
            for name in ("pydantic", "spacy", "torch", "transformers")
        },
        "omp_threads": os.environ.get("OMP_NUM_THREADS"),
        "torch_threads": os.environ.get("TORCH_NUM_THREADS"),
        "tokenizers_parallelism": os.environ.get("TOKENIZERS_PARALLELISM"),
    }


def _write_once(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")


def run_benchmark(*, suite: Path, backend, backend_name: str,
                  case_ids: tuple[str, ...] | None,
                  repetitions: int, warmups: int,
                  model_dir: Path | None = None,
                  model_name: str | None = None,
                  construction_ms: float | None = None) -> tuple[dict, dict]:
    """Use same real backend across warmup and timed runs; no oracle-label input."""
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("repetitions must be 1..10")
    if type(warmups) is not int or not 0 <= warmups <= 5:
        raise ValueError("warmups must be 0..5")
    if backend_name not in ("spacy", "deberta", "ollama"):
        raise ValueError("unsupported backend")
    if not isinstance(getattr(backend, "backend_id", None), str):
        raise TypeError("backend must identify itself")
    provenance = _model_provenance(backend_name=backend_name, backend=backend,
                                    model_dir=model_dir, model_name=model_name)
    decision_thresholds = _decision_thresholds(
        backend_name=backend_name, backend=backend
    )
    warmup_times = []
    warmup_errors = 0
    for _ in range(warmups):
        start = perf_counter()
        try:
            prediction = backend.predict(
                source="The lights are on.", quote="The lights are on"
            )
            if not isinstance(prediction, ProbeLabels):
                raise TypeError("unvalidated warmup response")
        except Exception:
            # No raw exception messages; invalid JSON is an observed failure.
            warmup_errors += 1
        warmup_times.append((perf_counter() - start) * 1000)

    reports = []
    durations = []
    for _ in range(repetitions):
        start = perf_counter()
        result = evaluate_probe(suite, backend=backend, case_ids=case_ids)
        durations.append((perf_counter() - start) * 1000)
        reports.append(result)
    first = reports[0]
    comparable_fields = ("sample_cases", "sample_claims", "suite_sha256",
                         "prompt_version", "reference_mode", "backend")
    for result in reports[1:]:
        if any(result[key] != first[key] for key in comparable_fields):
            raise ValueError("different cases or backend across repetitions")
    # Repetitions may disagree! Never merge into a falsely precise static
    # accuracy score; preserve differences in the separate profile.
    metrics = [report["by_dimension"] for report in reports]
    return first, {
        "version": VERSION,
        "reference_mode": first["reference_mode"],
        "annotation_status": first["annotation_status"],
        "suite_sha256": first["suite_sha256"],
        "prompt_version": first["prompt_version"],
        "backend": first["backend"],
        "model": provenance,
        "nli_decision_thresholds": decision_thresholds,
        "host": _host(),
        "sample_cases": first["sample_cases"],
        "sample_claims": first["sample_claims"],
        "case_ids": [row["id"] for row in first["cases"]],
        "construction_ms": (round(construction_ms, 3)
                            if construction_ms is not None else None),
        "warmup": {
            "count": warmups, "errors": warmup_errors,
            "durations_ms": [round(v, 3) for v in warmup_times],
        },
        "runs": [{
            "index": index + 1, "duration_ms": round(duration, 3),
            "error_claims": report["error_claims"],
            "median_claim_inference_ms": report["median_claim_inference_ms"],
            "total_inference_ms": report["total_inference_ms"],
            "by_dimension": metrics[index],
        } for index, (duration, report) in enumerate(zip(durations, reports))],
        "timing": {
            "median_suite_ms": round(median(durations), 3),
            "p95_suite_ms": _percentile(durations, .95),
            "max_suite_ms": round(max(durations), 3),
        },
        "peak_python_process_rss_kb": (
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if platform.system() == "Linux" else None
        ),
        "limits": [
            "Synthetic author-draft labels are not independently reviewed.",
            "Oracle claim text is provided to model; extraction is not scored.",
            "First run is NOT an independent holdout or unbiased model accuracy.",
            "Python RSS does not include the separate Ollama server process.",
            "No background routing, data retention, training or Memory writes.",
        ],
    }


def main(argv=None, *, injected_backend=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c14d_semantic_drafts.json"))
    parser.add_argument("--backend", choices=("spacy", "deberta", "ollama"),
                        required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--min-score", type=float, default=.65)
    parser.add_argument("--min-margin", type=float, default=.10)
    parser.add_argument("--timeout-s", type=float, default=90)
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--report", required=True, type=Path,
                        help="Write-once v14E report accepted by paired comparator")
    parser.add_argument("--profile", required=True, type=Path,
                        help="Write-once real-backend repeated-run timings")
    args = parser.parse_args(argv)
    if args.report == args.profile or args.report.exists() or args.profile.exists():
        parser.error("report and profile must be distinct, unused paths")
    try:
        start = perf_counter()
        backend = injected_backend
        if backend is None:
            if args.backend == "spacy":
                backend = SpacyStructuralProbe()
            elif args.backend == "deberta":
                if args.model_dir is None:
                    raise ValueError("--model-dir required")
                backend = DebertaNliProbe(
                    args.model_dir, device=args.device,
                    min_score=args.min_score, min_margin=args.min_margin
                )
            else:
                if not args.model:
                    raise ValueError("--model required")
                backend = OllamaJsonProbe(model=args.model, url=args.ollama_url,
                                          timeout_s=args.timeout_s)
        construction_ms = (perf_counter() - start) * 1000
        scored, profiled = run_benchmark(
            suite=args.suite, backend=backend, backend_name=args.backend,
            case_ids=tuple(args.case_ids) if args.case_ids else None,
            repetitions=args.repetitions, warmups=args.warmups,
            model_dir=args.model_dir, model_name=args.model,
            construction_ms=construction_ms,
        )
        _write_once(args.report, scored)
        _write_once(args.profile, profiled)
    except (ValueError, TypeError, OSError, ImportError, RuntimeError,
            json.JSONDecodeError):
        parser.error("invalid input, missing local model, or benchmark failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
