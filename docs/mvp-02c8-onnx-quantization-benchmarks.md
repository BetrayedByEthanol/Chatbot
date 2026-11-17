# MVP-02C.8 — Local ONNX FP32/INT8 NLI evidence and parity follow-up

**Status:** Benchmark evidence only; provisional candidate, **not** a production
model selection or a Memory-domain policy. No classifier may automatically
modify, supersede, or delete persona memories.

## Scope and provenance

Evaluated two existing local checkpoints on CPU with native PyTorch,
exported ONNX FP32, and ONNX Runtime dynamically quantized U8U8 weights:

- `finecat-nli-v2-xl` (native config SHA256
  `2f5ad401a14cb1654be9d3b7ff2b0c6528d84334603134414734ac49ceb7d757`)
- `mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` (native config SHA256
  `bc7b85f164a17c1b007d87fb99d3676f4a4d2e6511d2288dd3f5334dc0f34e6b`)

Identical local evaluation suites were used in the native and ONNX comparisons:

| Suite | Cases (E / N / C) | SHA256 |
|---|---:|---|
| `scenarios/mvp02c5_nli_comparison.json` | 54 (18/18/18) | `3267853044e4a899178e15062dc5184f83246af598d1b1d5392968c3e1a22df8` |
| `scenarios/mvp02c7_nli_threshold_diagnostics.json` | 120 (24/72/24) | `a48ee65c36e4346551a95ada39d482ea421f9b30cea03b0d49082977ee6b9e51` |

Both datasets are synthetic; the 120-case labels remain **draft** and require
independent review. Neither suite is a blinded, population-representative
holdout. Reported false-alert rates therefore describe only these cases.

Native reference reports: `mvp02c6-final-nli-comparison.json` (54 cases) and
`mvp02c7-native-threshold.json` (120 cases). Local ONNX reports:
`finecat-onnx-parity.json`, `DEBERTA-onnx-parity.json`, with additional
comparison repetitions in `finecat-onnx-comparison.json`,
`mDEBERTA-onnx-comparison.json`, and `DEBERTA-onnx-comparison.json`.
These reports were supplied as user-side benchmark artifacts and have **not**
been committed to the repository. Exported model binaries are also not tracked.

## Method

- ONNX Runtime **1.32.0**, CPU provider, two inference threads;
  one warmup pass, two measured repetitions, isolated sequential sessions.
- Same paired-text tokenizer and `max_length=256` used by the benchmark
  adapters; verified NLI label order **entailment=0, neutral=1,
  contradiction=2** for both checkpoints.
- `onnx/model.onnx` is the FP32 inference graph; the examined quantized file
  is `onnx/model_quint8_avx2.onnx`. Both sizes below include external-data
  sidecars.
- INT8 generated with ONNX Runtime `quantize_dynamic`, unsigned weights
  (`QuantType.QUInt8`), `MatMul`/`Gemm` operators,
  `per_channel=False`, `reduce_range=False`,
  `use_external_data_format=True`.
- ONNX comparison was run through a **local evaluation helper**,
  `onnx_nli_compare.py`, which is **not yet tracked in the repository**.
  This document captures the results; the full export/quantization and
  native-parity workflow still needs a separately reviewed reproducible
  script and exported-binary digests.
- A contradiction *alert* here is solely the uncalibrated softmax
  `score["contradiction"] >= 0.90` against draft labels, **not** an
  authorized contradiction finding or calibrated likelihood.

## Combined diagnostic results (54 + 120 = 174 cases)

There are 42 labeled contradictions and 132 labeled noncontradictions.
TP, FP, FN, and TN below are computed at the fixed 0.90 score cutoff, not
from the three-way argmax classification.

| Variant | Correct / 174 | TP | FP | FN | TN | Alert precision | Alert recall |
|---|---:|---:|---:|---:|---:|---:|---:|
| FineCat ONNX FP32 | 138 | 42 | 6 | 0 | 126 | 87.50% | 100.00% |
| FineCat ONNX INT8 U8U8 | 137 | 41 | 6 | 1 | 126 | 87.23% | 97.62% |
| mDeBERTa ONNX FP32 | 143 | 39 | 13 | 3 | 119 | 75.00% | 92.86% |
| mDeBERTa ONNX INT8 U8U8 | 156 | 14 | 0 | 28 | 132 | 100.00% | 33.33% |

**Interpretation:** mDeBERTa INT8 improves overall three-way accuracy by
classifying many previously misclassified neutral examples correctly,
but at the fixed 0.90 cutoff it misses 28/42 contradictions. This is
a disqualifying regression for the *specific evaluated contradiction-alert
behavior*, not proof that the quantized checkpoint is unusable at every
possible calibrated operating point.

FineCat INT8 preserves 41/42 threshold alerts and the same six false
alerts as FineCat FP32. This makes FineCat INT8 the **provisional candidate**
for *advisory-only* contradiction assessment, conditional on the parity
investigation and independent evaluation below.

## CPU cost (120-case suite, two threads)

| Variant | Median per pair | P95 per pair | ONNX artifact bytes |
|---|---:|---:|---:|
| FineCat FP32 | 200.187 ms | 249.899 ms | 8,251,609,947 |
| FineCat INT8 | 141.261 ms | 204.210 ms | 1,303,788,414 |
| mDeBERTa FP32 | 31.749 ms | 37.277 ms | 1,116,156,872 |
| mDeBERTa INT8 | 23.965 ms | 32.121 ms | 916,662,197 |

Times above use the *parity* report run, rather than mixing medians from
different repeat runs. These are sequential synthetic CPU measurements, not
p95 latency under concurrent Ollama or background workload. Native PyTorch
latencies are not directly comparable to ONNX latencies. FP32 ONNX artifacts
are substantially larger than native checkpoint files; the exact effect
of checkpoint weight dtypes versus exporter conversion has not been
verified.

## FP32 export parity against native PyTorch

| Model | Argmax differences (54 / 120) | Threshold changes (54 / 120) | Max absolute softmax-score delta (54 / 120) |
|---|---:|---:|---:|
| FineCat FP32 | 1 / 1 | 0 / 0 | 0.192443 / 0.171508 |
| mDeBERTa FP32 | 0 / 0 | 0 / 0 | 0.010774 / 0.009935 |

FineCat's changed argmax cases are `N02` and `HN026`. The score drift is
not confined to those two cases: `N08` has a 0.192443 absolute score change
in the 54-case suite, and `HE011` has a 0.171508 change in the 120-case
suite. All 174 FineCat FP32 **0.90 alert decisions** matched native PyTorch;
this does **not** establish close numerical/logit parity.

mDeBERTa FP32 matches all 174 native argmax and fixed-threshold decisions,
with a maximum absolute score difference below 0.011.

### INT8 threshold regression: FineCat `HC010`

> The same Monday meeting starts at 14:00 vs. 16:00.

| Runtime | Contradiction score | Alert at 0.90? |
|---|---:|---|
| Native PyTorch | 0.928993 | Yes |
| FineCat ONNX FP32 | 0.902350 | Yes |
| FineCat ONNX INT8 | 0.896838 | **No** |

Although INT8's argmax remains contradiction, this is one false negative
**at the fixed threshold**. It demonstrates why score/parity checks are
required in addition to class agreement.

Quantized mDeBERTa exhibits much larger output drift and threshold changes
(9 in the 54-case suite, 29 in the 120-case suite versus native). Do not
infer runtime safety from its increased overall accuracy.

## Follow-up gates and limits

1. **Investigate FineCat FP32 discrepancies before integrating it into
   production.** Repeat `N02`, `HN026`, `N08`, `HE011`, and `HC010`
   with the same token IDs, masks, truncation, label order and output
   normalization for native and ONNX. Compare raw logits, not only
   rounded softmax scores. Check model evaluation mode, model/tokenizer
   revision, original weight dtype, export conversion, and runtime versions.
   Keep this investigation read-only.
2. Capture ONNX graph and external-weight SHA256 digests, exporter/quantizer
   versions, commands, native dtype and verified custom-code revisions.
   Add the missing locally used ONNX evaluation helper as a separate
   reviewable, tested tooling change if repeatability is required.
3. Independently review ambiguous labels; use an unseen, realistic holdout
   with sufficient neutral, temporal, goal/intention, language and
   preference cases. **Do not choose a production threshold using these
   same diagnostic examples.**
4. Evaluate candidate-pair selection and temporal/modality compatibility
   *before* running NLI. Run integration acceptance tests for false alerts,
   cancellation, and recovery under realistic background load.
5. Keep NLI output as provenance-linked **advisory evidence only**.
   Memory-domain commands, authorization and reconciliation still own all
   persistent changes.

**Decision:** Document FineCat INT8 as a promising *experimental* offline
candidate; **defer production adoption** until FP32 parity and independent
validation are resolved. No changes to runtime model routing or Memory
authority are approved by these benchmark results.
