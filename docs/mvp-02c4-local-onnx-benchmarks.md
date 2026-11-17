# MVP-02C.4 — Portable local ONNX benchmarking (first vertical slice)

**Decision:** Use Python ONNX Runtime, **not Ollama**, for small encoder models.
Ollama continues to handle the conversational generative LLM. This branch is
**benchmark-only**: no production classifier activation, async workers,
memory admission, topic assignment, or new state-owning subsystem.

## Install on the machine with the models (Threadripper now, Ryzen later)

From a trusted clone of this repository:

```bash
python -m pip install -e ".[onnx-bench,test]"
python scripts/benchmark_local_onnx.py \
  --models-root "$HOME/models" \
  --role all --precision int8 --intra-op-threads 2 \
  --report artifacts/mvp02c4-threadripper-int8.json
python scripts/benchmark_local_onnx.py \
  --models-root "$HOME/models" \
  --role all --precision fp32 --intra-op-threads 2 \
  --report artifacts/mvp02c4-threadripper-fp32.json
```

The script loads local tokenizers and ONNX weights only, never downloads a
checkpoint, never contacts Hugging Face and never starts Ollama, Docker or
PostgreSQL. Do not register these models in Ollama or create Modelfiles.
Only the optional `onnx-bench` extra installs NumPy, ONNX Runtime (CPU),
Transformers and SentencePiece; the production dependency set is unchanged.

Currently supported:

| Role | Local directory | Output |
|---|---|---|
| Sentence embeddings | `all-MiniLM-L6-v2` | L2-normalized, mean-pooled vectors |
| NLI | `nli-deberta-v3-base` | entailment / neutral / contradiction probabilities |

The MiniLM encoder ONNX output is **token embeddings**: an attention-mask mean
pooling and vector normalization step is required. For NLI we resolve labels
from the local `config.json` and reject `LABEL_0` etc. rather than guess
which output represents contradiction. Only when *independently verified*
against the checkpoint's model card may the three exact indices be supplied:

```bash
python scripts/benchmark_local_onnx.py --models-root "$HOME/models" \
  --role nli --nli-label-order contradiction neutral entailment
```

This example index order is illustrative, **not verified for your checkout**.

## Portable precision selection

`--precision auto` prefers a compatible INT8 export and falls back to
`onnx/model.onnx`. `--precision int8` requires a compatible INT8 ONNX file
and fails instead of falsely reporting an FP32 run. `--precision fp32` always
loads the baseline ONNX file.

Compatible architecture-specific files are selected from **process-visible**
CPU flags, which matters in VMs or containers:

- Ryzen 9 7950X with AVX-512 VNNI: `model_qint8_avx512_vnni.onnx`;
- Threadripper PRO 3945WX with AVX2: `model_quint8_avx2.onnx`;
- without a supported quantized variant: `model.onnx` for auto/FP32.

For each role, one ONNX Runtime `CPUExecutionProvider` session is created.
The bounded `--intra-op-threads` option controls CPU concurrency.
There is no CUDA dependency, subprocess service or model copy in the repo.

## Output and interpretation

JSON reports include actual resolved ONNX filename, requested precision,
ONNX Runtime version, CPU flags, model weight bytes, load time, median and
P95 warm inference time, process peak RSS and synthetic example predictions.

**Limits:** Three synthetic NLI sentence-pair checks and three embedding
triplets are only plumbing smoke tests. The `smoke_correct` number is NOT
general accuracy, calibration, contradiction precision, or memory retrieval
quality. Results compare local hardware/runtime on identical inputs, but
are not equivalent to foreground or concurrent performance under Ollama load.

No output is written unless `--report` is specified; existing reports are
never overwritten. Model directories are never written.

## Remaining work before production

1. Run real-model FP32 and INT8 on the Threadripper and inspect output.
2. Expand labeled evaluation and derive task-specific quality thresholds.
3. Add model-specific zero-shot and emotion adapters (current checkpoints
   need different exports/quantizations). Keep them optional and read-only.
4. Compare CPU parallelism against sequential runs and Ollama latency.
5. Run identical benchmark on Ryzen after setup.
6. Integrate a proven adapter into the existing Stage A orchestrator only
   after accuracy, latency and domain-ownership review.

No code path currently uses these model outputs to update Memories, Emotions,
Goals or Conversation. We do not assume an NLI contradiction means an approved
memory correction, and embeddings are used solely for this benchmark.
