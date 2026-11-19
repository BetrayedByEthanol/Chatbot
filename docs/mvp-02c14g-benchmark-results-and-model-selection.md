# MVP-02C.14G — Actual benchmark analysis and model selection criteria

**Status:** offline evidence review; **NO MODEL SELECTED**. No production
semantic router, classifier authorization, Memory writes, or model training.

## 1. Evidence inspected

Source: GitHub Actions [MVP-02C.14F run 44516887227](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/44516887227)
on commit `577801f9a02f664f7f515263565d574f436447bf` (completed successfully,
2026-11-19). Artifact ID `14525339282`, artifact name
`mvp02c14f-real-spacy-qwen-benchmark`, expires **2026-12-03**.
Archive digest recorded by GitHub:
`sha256:aadded6a594f94794fc9a6d8e0454fcabc82b96b933afd626db3b6887b5e1deb`.

The five archived text-free JSON files were inspected directly.
All runs used the same v14D provisional suite SHA-256
`ef9745f194ff22e2f8f96203719e5119ab40fb4a605eebf00f20f0dc7b69c610`,
15 synthetic cases, 18 author-draft claim spans, and
`mvp02c14e.span_conditioned.v1`.

Relevant input SHA-256s (raw JSON bytes):

| Artifact | SHA-256 |
|---|---|
| `spacy-scores.json` | `1b1c427fe8348340faaf5c6f6e839de8c0d000af28d5b6efbced335748f4bcd7` |
| `spacy-profile.json` | `a07137673a132290b29117c147a10d4288f3c46281798f3f9ad60f41399e5027` |
| `qwen-scores.json` | `2d95853781d8d271188c8e69fa20bf7a335541d5abfe79d9299ad3085b244317` |
| `qwen-profile.json` | `ac4f57375fb9789074114b82dbb9a5327e4460fe10d7ceddea991e5edcdcc24d` |
| `spacy-qwen-paired.json` | `e08be98db485e812a73408eade6d5abfcd40bbb0c0f171424562c2b5e03087cc` |

These labels are **author drafts, not independent gold**. Claims were
selected from reference spans and passed to every classifier. None of this
measures extracting claims, qualifiers, relations, correction targets,
conversation continuity, or Memory safety.

## 2. Findings from actual real-model execution

**spaCy baseline** = `spacy.blank_en.lexical_cues.v1` (tokenizer and
explicit lexical rules, not a trained semantic classifier). **Qwen** =
`qwen2.5:0.5b`, model digest
`a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`,
running locally in Ollama.

| Dimension | Eligible draft labels | spaCy supported | spaCy matched draft | Qwen supported |
|---|---:|---:|---:|---:|
| Statement type | 18 | 0 | 0 | 0 |
| Certainty | 18 | 1 | 1 | 0 |
| Polarity | 18 | 6 | 6 | 0 |
| Habituality | **1** | 1 | 1 | 0 |
| Conditionality | 18 | 0 | 0 | 0 |
| Hypotheticality | 18 | 1 | 1 | 0 |
| Temporal references | 18 | 5 | 4 | 0 |

The 17 remaining habituality labels were **unknown reference values**,
not negatives. spaCy's 100% draft agreement on small *supported subsets*
cannot be interpreted as overall correctness: its statement type and
conditionality coverage was 0%. One of its five supported temporal sets
disagreed with the draft; the existing text-free aggregates cannot identify
which claim without an authorized, separate per-case investigation.

**Critical Qwen observation:** **18/18 model predictions failed** in
*each of two measured runs*; warmup also failed. Thus Qwen produced
**zero valid classifications**, not 18 semantic mistakes or an accuracy
of 0%. The precise failure class was **not recorded by v14F**. We cannot
conclude whether the cause was structured-output schema violations, JSON
parsing, model response truncation, server failure, or another error.
A green benchmark workflow only proves that the experimental process ran.

| Measurement | spaCy | Qwen2.5 0.5B |
|---|---:|---:|
| Errors per measured run | 0 / 18, 0 / 18 | **18 / 18, 18 / 18** |
| Warmup errors | 0 / 1 | 1 / 1 |
| Median per-claim wall time (run 1) | 0.050 ms | 1,532.294 ms |
| Median full-suite wall time (2 runs) | 1.427 ms | 28,424.756 ms |
| Model/probe construction | 645.219 ms | 5.252 ms (HTTP client only) |

Those timings are on a **4-logical-CPU GitHub Linux runner** with Python
3.12.16 and spaCy 3.8.18. Qwen inference and Docker server resources are
not included in the Python RSS statistic. A fast error response is **not**
a successful inference; Qwen's latency cannot be treated as acceptable
classification latency.

**DeBERTa: not measured** in this paired run; it requires a real,
previously installed local model. **Qwen2.5 7B: not measured**. Neither
may be ranked based on the 0.5B experiment.

## 3. Selection criteria (proposals, NOT approved policy)

Apply gates in order. No soft aggregate score, conditional agreement
percentage or single successful example can override an upstream failure.

| Gate | Proposed evidence threshold | Current result |
|---|---|---|
| **G0 — Provenance & compatibility** | Same cases/prompt, immutable model+tokenizer fingerprint, recorded NLI thresholds, no label leakage | spaCy/Qwen paired; DeBERTa absent |
| **G1 — Output reliability** | Repeated local runs with <=1% invalid/error outputs each; count malformed model output separately from abstention | Qwen **FAIL** (100% error); spaCy observed 0 errors |
| **G2 — Reviewed labels** | Independently adjudicated holdout; propose >=200 eligible annotated claims *per required dimension*, including negative/ambiguous cases | **BLOCKED** (15 author-written cases) |
| **G3 — Selective semantic utility** | Propose >=80% coverage and >=95% conditional agreement *per required dimension*, with uncertainty estimates and paired confidence comparisons against deterministic processing | **BLOCKED**; spaCy too little coverage; Qwen no valid output |
| **G4 — High-risk domain safety** | Separately verify safe abstention on negation, temporal shifts, contradictory claims, privacy, prior-message references, and correction target; domain owners retain admission authority | **UNTESTED** |
| **G5 — Runtime suitability** | Cold/warm p95 and CPU/RAM measured on intended hardware; bounded foreground latency and fallback; no DB locks around model inference | **UNTESTED** |
| **G6 — Explicit approval** | Independently reviewed selection and isolated shadow-mode routing PR, then validated memory/correction lifecycle with rollback | **NOT AUTHORIZED** |

The numerical G1–G3 values are **initial proposals**, not established
architecture targets. Review class prevalence, uncertainty bounds, cost,
latency budgets and failure cost before adopting them. Zero errors on a
15-case synthetic set does not establish 99% reliability.

## 4. Architectural outcome and required experiments

The v2.5.1 adaptive pipeline remains **deterministic for supported simple
cases**, permits selective model interpretation with abstention and
source-grounding, and reserves deeper inference for contexts where it adds
value. The persona must remain responsive; classifier outputs are
**advisory** and never authorize Memory mutation or correction.

Before considering a model:
1. Diagnose Qwen's **18/18** failures in an isolated local replay. Record
   a bounded, privacy-safe **error-class histogram** (e.g. JSON shape,
   Pydantic validation, timeout/transport) without raw model content. Do
   not call these semantic failures until valid predictions exist.
2. Rebenchmark actual intended Qwen and DeBERTa checkpoints with the
   **same adjudicated cases**, recorded tokenizer and threshold provenance.
3. Independently annotate a representative, stratified holdout. Cover
   compound claims, state changes vs conversational corrections, temporal
   context, and missing evidence. Keep evaluation data out of model prompts.
4. Separately evaluate claim extraction and qualifier/relation preservation
   using the v14D evaluator. Measure both **supported and abstained** claims.
5. Benchmark on target hardware with the real conversation foreground
   workload, not just a model-only GitHub runner.
6. Only then propose a **shadow-mode** adapter experiment in a new PR;
   no silent changes to existing domain-owned persistence or access controls.

## 5. Executable analysis

`scripts/analyze_semantic_benchmark_results.py` validates paired v14F
score/profile contracts, v14E dimension arithmetic, per-run errors, case IDs
and versioned provenance. It emits a write-once **text-free** analysis
containing coverage, conditional *draft* agreement, error fractions, repeated
run drift, model-selection blockers, and explicitly **NO_MODEL_SELECTED**.

From an extracted v14F artifact:

```bash
PYTHONPATH=src python scripts/analyze_semantic_benchmark_results.py \
  --pair evidence/spacy-scores.json evidence/spacy-profile.json \
  --pair evidence/qwen-scores.json evidence/qwen-profile.json \
  --run-id 44516887227 \
  --report evidence/mvp02c14g-analysis.json
```

The v14F real benchmark workflow now generates the same analysis on each
benchmark run, shows warnings in the GitHub job summary when outputs are
invalid, and archives the text-free JSON. Its successful **execution** does
not mean a model has passed the promotion gates.

### Decision register

- **D14G-1 confirmed:** Real Qwen2.5 0.5B classification evidence is
  **technically invalid** (36/36 measured claim attempts failed over two
  runs); root cause unverified.
- **D14G-2 confirmed:** The spaCy cue probe yields a fast, narrow baseline,
  not broad semantics or a competing production semantic model.
- **D14G-3 confirmed:** No deployed model winner can be selected from the
  15 provisional author-draft cases. Both models remain evaluation-only.
- **D14G-4 proposed:** G0–G6 model selection criteria above; numerical
  thresholds require explicit review before production adoption.
