# MVP-02C.9 — FineCat FP32 parity investigation and regression gate

**Decision:** The existing FineCat ONNX FP32 export is numerically equivalent to
native PyTorch **when evaluated with the export-compatible Transformers 4.57.6
implementation**. Version-dependent native behavior explains the previously
reported ONNX-vs-PyTorch discrepancies. This is a benchmark/tooling decision,
**not** approval of FineCat INT8 for production or a Memory policy change.

## Investigation evidence

The 174 diagnostic examples consist of the unchanged 54-pair baseline
(`scenarios/mvp02c5_nli_comparison.json`) and 120-pair draft expansion
(`scenarios/mvp02c7_nli_threshold_diagnostics.json`). Five cases were
isolated first: `N02`, `N08`, `HN026`, `HE011` and `HC010`.

The previously supplied `mvp02c9-finecat-original.json`,
`mvp02c9-finecat-fp32cast.json`, and `mvp02c9-finecat-full.json` recorded:

- Local model config SHA256:
  `2f5ad401a14cb1654be9d3b7ff2b0c6528d84334603134414734ac49ceb7d757`.
- The loaded checkpoint has **1,031,269,123 FP32 parameter elements** already;
  explicitly casting to FP32 changes nothing. The native weight dtype is not
  the cause of the logits drift.
- Tokenizer outputs are identical for all 174 examples. `CrossEncoder.predict`
  and direct PyTorch inference agree for all 174.
- Under Transformers **5.23.0**, direct PyTorch vs ONNX FP32 has **2/174**
  three-way argmax disagreements (`N02`, `HN026`), **0/174** differences at
  the fixed 0.90 contradiction-score mark, maximum probability delta
  **0.19244308**, and maximum centered-logit delta **0.67090708**.
- Re-running the five examples under Transformers **4.57.6** gave matching
  PyTorch and existing ONNX FP32 logits to the printed precision.
- The final **174/174** comparison under Transformers **4.57.6** yielded:

| Check | Result |
|---|---:|
| Maximum absolute raw-logit difference | `2.6226043701171875e-06` |
| Maximum absolute softmax-probability difference | `4.76837158203125e-07` |
| Argmax prediction changes | **0** |
| Contradiction `>=0.90` mark changes | **0** |

**Interpretation:** The export is validated against the matching
Transformers 4.57.6 native implementation on these examples. This establishes
a version-conditioned difference, **not** the specific internal Transformer
operator responsible. The earlier 5.23 native scores must not be treated as
reference scores for this 4.57.6-based ONNX export.

## Optional offline regression command

The repo test suite covers *comparison logic and fail-closed CLI behavior*
without downloading or loading multi-gigabyte models. The real-model parity
command is **manual/optional** and runs only on a machine with a reviewed local
FineCat checkpoint and its existing ONNX FP32 export.

Use an isolated export-compatible venv with `transformers==4.57.6`, PyTorch,
ONNX Runtime, NumPy, and the local reviewed model code. Do **not** pin the
entire persona runtime to this version: this is the **export/parity toolchain**.

```bash
PYTHONPATH=src python scripts/check_finecat_fp32_parity.py \
  --model-dir "$HOME/models/nli/finecat-nli-v2-xl" \
  --allow-reviewed-custom-code \
  --all --threads 2 \
  --report artifacts/mvp02c9-fp32-parity-regression.json
```

Without `--all`, the script checks just the five named regression cases.
It fails when the Transformers version is not **exactly 4.57.6**, when the
fixed FineCat label order is wrong, when data or ONNX inputs are invalid,
when any class/threshold decision changes, or when the maximum per-case
**absolute raw-logit delta exceeds 1e-4** or **probability delta exceeds 1e-5**.
These tolerances exceed the measured matched-runtime deltas without excusing
the original 0.19 score drift. The command never overwrites existing reports;
custom model code requires explicit opt-in after local inspection.

No model weights, user conversations, or benchmark JSONs are committed. The
script does not download/export models, modify persona state, or authorize
memory changes. Model graphs and external-weight SHA256 digests and the reason
for the approximately 8.25 GB FP32 export size remain unverified provenance
questions, not evidence that the export logits are wrong.

## Remaining decision gates

1. **Keep FineCat INT8 provisional.** The previously observed `HC010`
   threshold crossing remains a quantization-level issue, distinct from the
   resolved FP32 export parity. Use ONNX FP32, not Transformers 5.23 PyTorch,
   as the INT8 comparison baseline.
2. Independently review the 120 draft labels and evaluate realistic memory
   conflict pairs, temporal scopes, intentions vs outcomes and languages on
   unseen cases. Do not tune the 0.90 threshold against this diagnostic set.
3. If the export toolchain or model revision changes, run the matching
   native-vs-ONNX parity regression again before comparing quantized results.
4. NLI is **advisory evidence** only. Memory-domain authorization and
   reconciliation remain the sole authority for persistent memory updates.

**No new production model routing, state ownership, or database schema change.**
See [MVP-02C.8](mvp-02c8-onnx-quantization-benchmarks.md) for the preceding
FP32/INT8 benchmark and limitations.
