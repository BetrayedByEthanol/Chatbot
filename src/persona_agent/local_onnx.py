"""Offline CPU ONNX inference for benchmark-only NLI and sentence embeddings.

This module is an optional leaf adapter: no models are downloaded, no memory/
conversation state is changed, and imports of heavy inference packages happen
only when an adapter is instantiated. Ollama remains the conversational model.
"""
from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Role = Literal["embedding", "nli"]
Precision = Literal["auto", "fp32", "int8"]
NLI_LABELS = frozenset({"entailment", "neutral", "contradiction"})


def cpu_instruction_flags(cpuinfo: str | None = None) -> frozenset[str]:
    """Only use flags visible to this process (VMs may mask host features)."""
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError:
            return frozenset()
    for line in cpuinfo.splitlines():
        if line.startswith(("flags", "Features")) and ":" in line:
            return frozenset(line.partition(":")[2].strip().split())
    return frozenset()


def select_onnx_file(
    model_dir: Path, *, precision: Precision = "auto",
    flags: frozenset[str] | None = None,
    machine: str | None = None,
) -> Path:
    """Choose a compatible file; never try AVX512 on an AVX2-only machine.

    FP32 is the portable fallback. Requiring INT8 fails explicitly instead
    of quietly claiming a quantized benchmark while loading FP32.
    """
    if precision not in ("auto", "fp32", "int8"):
        raise ValueError("precision must be auto, fp32, or int8")
    model_dir = Path(model_dir).expanduser().resolve()
    folder = model_dir / "onnx"
    flags = cpu_instruction_flags() if flags is None else flags
    machine = platform.machine().lower() if machine is None else machine.lower()
    variants: list[str] = []
    if precision != "fp32":
        if machine in ("x86_64", "amd64"):
            if "avx512_vnni" in flags and "avx512f" in flags:
                variants.append("model_qint8_avx512_vnni.onnx")
            if "avx512f" in flags:
                variants.append("model_qint8_avx512.onnx")
            if "avx2" in flags:
                variants.append("model_quint8_avx2.onnx")
        elif machine in ("aarch64", "arm64"):
            variants.append("model_qint8_arm64.onnx")
        for filename in variants:
            candidate = folder / filename
            if candidate.is_file():
                return candidate
        if precision == "int8":
            raise FileNotFoundError(
                f"No compatible INT8 ONNX variant in {folder} "
                f"for {machine}, flags={sorted(flags & frozenset({'avx2','avx512f','avx512_vnni'}))}"
            )
    fp32 = folder / "model.onnx"
    if not fp32.is_file():
        raise FileNotFoundError(f"Missing local ONNX FP32 model: {fp32}")
    return fp32


def nli_indices(
    config: dict, *, explicit_label_order: tuple[str, str, str] | None = None,
) -> dict[str, int]:
    """Read labels from local config, never assume an undocumented index order."""
    if explicit_label_order is not None:
        labels = [label.casefold() for label in explicit_label_order]
        if len(labels) != 3 or set(labels) != NLI_LABELS:
            raise ValueError("explicit NLI label order must be a permutation of entailment, neutral, contradiction")
    else:
        raw = config.get("id2label")
        if not isinstance(raw, dict) or len(raw) != 3:
            raise ValueError("NLI config must define exactly three id2label entries")
        try:
            labels = [str(raw[str(i)] if str(i) in raw else raw[i]).casefold() for i in range(3)]
        except (KeyError, TypeError) as exc:
            raise ValueError("NLI config id2label must cover indices 0,1,2") from exc
        if set(labels) != NLI_LABELS:
            raise ValueError(
                "NLI config has ambiguous labels; pass an explicit, verified --nli-label-order"
            )
    return {label: index for index, label in enumerate(labels)}


@dataclass(frozen=True)
class LocalONNXConfig:
    model_dir: Path
    role: Role
    precision: Precision = "auto"
    intra_op_threads: int = 2
    explicit_nli_label_order: tuple[str, str, str] | None = None

    def __post_init__(self):
        if self.role not in ("embedding", "nli"):
            raise ValueError("unsupported ONNX model role")
        if self.intra_op_threads < 1 or self.intra_op_threads > 32:
            raise ValueError("intra_op_threads must be 1..32")
        if self.role != "nli" and self.explicit_nli_label_order is not None:
            raise ValueError("NLI label override only applies to NLI")


class LocalONNXModel:
    """One local CPU session. Instantiated once per benchmark worker."""

    def __init__(self, config: LocalONNXConfig):
        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                'Optional inference dependencies missing: pip install -e ".[onnx-bench]"'
            ) from exc

        self.config = config
        self.np = np
        self.ort_version = ort.__version__
        model_dir = Path(config.model_dir).expanduser().resolve()
        self.model_file = select_onnx_file(model_dir, precision=config.precision)
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_dir), use_fast=True, local_files_only=True,
            trust_remote_code=False,
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = config.intra_op_threads
        self.session = ort.InferenceSession(
            str(self.model_file), sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        if "CPUExecutionProvider" not in self.session.get_providers():
            raise RuntimeError("CPU ONNX execution provider unavailable")
        self.labels = None
        if config.role == "nli":
            source = model_dir / "config.json"
            if not source.is_file():
                raise FileNotFoundError(f"Missing NLI model config: {source}")
            self.labels = nli_indices(
                json.loads(source.read_text(encoding="utf-8")),
                explicit_label_order=config.explicit_nli_label_order,
            )

    def _forward(self, *texts: list[str]):
        encoded = self.tokenizer(
            *texts, padding=True, truncation=True, max_length=256,
            return_tensors="np",
        )
        # Feed exactly the inputs required by the ONNX graph. Some DeBERTa
        # exports do not accept token_type_ids; others explicitly require it.
        outputs = {}
        for node in self.session.get_inputs():
            name = node.name
            value = encoded.get(name)
            if value is None and name == "token_type_ids":
                value = self.np.zeros_like(encoded["input_ids"])
            if value is None:
                raise ValueError(f"Tokenizer did not provide ONNX input {name!r}")
            if node.type == "tensor(int64)":
                value = self.np.asarray(value, dtype=self.np.int64)
            elif node.type == "tensor(int32)":
                value = self.np.asarray(value, dtype=self.np.int32)
            outputs[name] = value
        return self.session.run(None, outputs), encoded

    def nli(self, premise: str, hypothesis: str) -> dict[str, float]:
        if self.config.role != "nli" or self.labels is None:
            raise ValueError("NLI requires the NLI model role")
        arrays, _ = self._forward([premise], [hypothesis])
        logits = self.np.asarray(arrays[0], dtype=self.np.float64)
        if logits.shape != (1, 3):
            raise ValueError(f"Unexpected NLI logits shape: {logits.shape}")
        adjusted = logits[0] - logits[0].max()
        probs = self.np.exp(adjusted)
        probs /= probs.sum()
        return {name: float(probs[i]) for name, i in self.labels.items()}

    def embed(self, texts: list[str]):
        """Mean-pool masked token embeddings and L2-normalize each vector."""
        if self.config.role != "embedding":
            raise ValueError("Embeddings require the embedding model role")
        if not texts:
            raise ValueError("at least one text required")
        arrays, encoded = self._forward(texts)
        hidden = self.np.asarray(arrays[0], dtype=self.np.float32)
        mask = self.np.asarray(encoded["attention_mask"], dtype=self.np.float32)
        if hidden.ndim != 3 or hidden.shape[:2] != mask.shape:
            raise ValueError("Embedding ONNX output must be [batch, sequence, hidden]")
        mask = mask[:, :, None]
        pooled = (hidden * mask).sum(axis=1) / self.np.maximum(mask.sum(axis=1), 1e-9)
        lengths = self.np.linalg.norm(pooled, axis=1, keepdims=True)
        return pooled / self.np.maximum(lengths, 1e-9)
