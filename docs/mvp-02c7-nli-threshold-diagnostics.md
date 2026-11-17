# MVP-02C.7 — Independent draft NLI diagnostics and fixed 0.90 alert mark

**Scope:** Offline evaluation only. No production NLI routing, policy change,
memory mutation, downloads, new execution infrastructure or ONNX conversion.

## Why this before ONNX optimization

The original 54-pair set exposed high-confidence false contradictions.
The MVP-02C.6 native runs found that multilingual mDeBERTa led on accuracy
(50/54), while FineCat had zero false contradiction scores >=0.90 in that
small sample (despite seven incorrect neutral argmax classifications). That is
not enough to declare either model safe: threshold performance could change
on a wider set, and quantization can change the score distribution.

## New diagnostics

`scenarios/mvp02c7_nli_threshold_diagnostics.json` has **120 new** sentence
pairs, separate from the original 54: **72 neutral, 24 entailment and 24
contradiction**. Slices cover unrelated facts, time changes, intentions versus
outcomes, hypothetical statements, ownership versus preference, quantifiers,
same-time contradictions, and German/cross-language pairs.

**Labels are newly drafted and require independent review.** The cases were
written after seeing previous model failures, so this is not a blinded test set.
No test accuracy represents expected real-world error rates.

The earlier 54 cases are unchanged as a regression baseline.

## Predeclared threshold behavior

`summarize_nli` now additionally marks **each case** with:

- `contradiction_at_threshold`: true only when the model's uncalibrated
  contradiction *score* is >=0.90 (not simply because contradiction is argmax).
- `contradiction_threshold_outcome`: TP, FP, FN or TN against draft gold labels.

A new `contradiction_threshold` aggregate reports TP/FP/FN/TN, precision,
recall, flagged count and false-alert rate on noncontradictions. Legacy argmax
metrics and existing report fields remain intact.

**This mark is observational.** It is not an authorization to alter memory,
a calibrated probability, or an approved production cutoff. The purpose is
to expose the tradeoff between false alerts and missed contradictions at a
*fixed* threshold, not to tune a threshold to the test set.

## Run on already downloaded models

Install the project's existing benchmark/test dependencies and native model
runtime in your venv. Verify any locally supplied custom model code before
permitting it.

```bash
python -m pip install -e ".[onnx-bench,test]" torch sentence-transformers

python scripts/compare_local_nli_native.py \
  --models-root "$HOME/models/nli" \
  --models mDeBERTa-v3-base-xnli-multilingual-nli-2mil7 finecat-nli-v2-xl \
  --allow-reviewed-custom-code \
  --threads 2 --warmup 1 --repetitions 2 \
  --report artifacts/mvp02c7-native-threshold.json
```

Both models run **sequentially on CPU**. The resulting JSON includes every
input, score, per-case threshold mark, all metrics, configuration hash and
timings. Existing reports will not be overwritten. The script is offline
and never updates persona memory. Custom checkpoint code is opt-in;
`--allow-reviewed-custom-code` is not a substitute for code inspection.

For the multilingual model alone, omit FineCat and the custom-code flag.
Do not compare native PyTorch latency directly with earlier ONNX latency.

## Decision gate

1. Review/resolve uncertain gold labels before interpreting measured gains.
2. Compare **mDeBERTa** and **FineCat** on this additional diagnostic set.
   Prioritize false contradiction score alerts and missed actual contradictions,
   alongside breakdowns by temporal/pragmatic and German slices.
3. If a candidate is promising, optimize/export that checkpoint to ONNX
   **after** this evaluation, then verify FP32 class/logit parity and repeat
   this exact evaluation on INT8. Do not assume the 0.90 mark retains its
   native semantics after quantization.
4. Leave final production selection to a larger, representative, separately
   annotated evaluation and domain-owner checks.

This change intentionally makes **no** new memory authority or policy decision.
