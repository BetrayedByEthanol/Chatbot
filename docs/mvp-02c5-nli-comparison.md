# MVP-02C.5 — Diagnose and compare NLI DeBERTa-v3 base vs large

**Scope:** Offline **model comparison and diagnostics only**. No Hugging Face
downloads, network inference, additional services, memory modifications,
classifier-driven corrections, or production Conversation Orchestrator change.

## Why

The first Threadripper benchmark produced a likely false contradiction for
`I enjoy gardening.` -> `I work as a programmer.` with BOTH FP32 and INT8
base checkpoints. The classifier assigned contradiction about 99.997% / 99.46%
respectively (uncalibrated softmax). A bigger model might help, but its
behavior must be measured, not assumed.

Hugging Face `cross-encoder/nli-deberta-v3-base` and `...-large` model cards
use label order **0=contradiction, 1=entailment, 2=neutral**. The local adapter
already checks `config.json`; no label override is assumed or applied here.
Reference: https://huggingface.co/cross-encoder/nli-deberta-v3-base and
https://huggingface.co/cross-encoder/nli-deberta-v3-large

## Run on the Threadripper (already downloaded)

After checking out the PR branch, with existing benchmark extras installed:

```bash
python -m pip install -e ".[onnx-bench,test]"
python scripts/compare_local_nli.py \
  --models-root "$HOME/models" \
  --models nli-deberta-v3-base nli-deberta-v3-large \
  --precisions fp32 int8 \
  --intra-op-threads 2 --warmup 1 --repetitions 2 \
  --report artifacts/mvp02c5-threadripper-nli-comparison.json
```

The script compares **four locally available ONNX variants** with the exact
same sentence pairs and tokenization settings. On the Threadripper it selects
`onnx/model.onnx` for FP32 and `onnx/model_quint8_avx2.onnx` for INT8.
The Ryzen later selects compatible VNNI variants without modifying code.

If RAM is constrained, run one model at a time:

```bash
python scripts/compare_local_nli.py \
  --models-root "$HOME/models" --models nli-deberta-v3-large \
  --precisions int8 --repetitions 1 \
  --report artifacts/mvp02c5-large-int8-only.json
```

Reports are only written to **new** paths and contain the full synthetic test
sentences. Avoid replacing them with private conversation text.

## Diagnostic dataset and metrics

`scenarios/mvp02c5_nli_comparison.json` contains 54 hand-labeled,
synthetic sentence pairs: **18 entailments, 18 neutral pairs and 18
contradictions**, including temporal reference mismatches, preferences,
conditional intentions, habits, factual negation, and the original false
contradiction with reversed premise/hypothesis.

Reports include:

- Dataset SHA256, checkpoint/config identification, CPU capabilities, actual
  selected ONNX file and label indices;
- Graph input names/types, tokenizer class and paired-input separator count;
- Model load time, median/P95 **per sentence-pair** latency and full-suite time;
- Per-class precision, recall and F1, confusion matrix and slice/group outcomes;
- **False contradiction count/rate on noncontradiction cases**, plus a count of
  high-confidence false contradictions at an *illustrative* 0.90 threshold;
- Every model's prediction and raw softmax scores for each synthetic input,
  plus model-vs-model and precision-vs-precision disagreements/corrections.

Every pair is evaluated in the same order. Model runs execute sequentially
in one process. These timings **must not** be confused with Ollama-loaded
concurrent latency or independent peak memory consumption.

## Interpretation

This is a **curated diagnostic set**, not an independent test set and not a
statistically representative sample. Scores are model softmax, **not
calibrated probabilities of facts or memories being wrong**. A high accuracy
on these 54 inputs is necessary but **not sufficient** for safe memory
conflict detection. Some temporal/pragmatic judgments require human review.
False positive contradiction detections are particularly costly to the
persona; no automatic Memory updates are authorized.

If both FP32 and INT8 fail on identical cases, treat the error as possible
model/data/domain mismatch rather than blaming quantization. If model variants
disagree, inspect specific pairs before choosing a model.

**Next after collecting reports:**
1. Compare base vs large and precision deltas; inspect the original neutral
   failure plus temporal cases.
2. If neutral errors remain surprising, perform separate *PyTorch vs ONNX*
   logits/paired-tokenizer parity verification for those exact checkpoints.
   This PR only checks ONNX input and local label mapping, NOT reference parity.
3. Review/edit ambiguous synthetic gold labels, then expand toward a
   representative, independently labeled and calibrated evaluation set.
4. Consider NLI only as evidence assessment; temporal scope, provenance and
   domain ownership remain necessary before changing Memory.
