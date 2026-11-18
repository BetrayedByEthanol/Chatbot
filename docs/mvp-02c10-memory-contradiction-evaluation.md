# MVP-02C.10 — Memory contradiction evaluation (offline diagnostic)

**Scope:** Read-only evaluation of a provisional local FineCat ONNX FP32 and
INT8 NLI checkpoint against **94 newly drafted synthetic memory-claim pairs**.
There is no memory extraction, stored-memory comparison, model routing change,
approved production threshold, new database table, or new service.

**Architectural authority:** Persistent Persona Agent Architecture v2.5.1
§19.3 (Memory), §20.3 (clarifications/contradictions), and existing Memory
owner/correction contracts. MVP-02C.9 established FP32 export parity with
Transformers 4.57.6; the earlier native Transformers 5.23 scores should **not**
serve as INT8 parity reference.

## Evaluation questions

1. Can FineCat identify direct same-subject/same-time contradictions without
   generating excessive false contradiction alerts?
2. Does it confuse *changed circumstances* with current conflict (past vs
   present; intentions vs actions; hypotheticals vs assertions)?
3. How do attribution, quotes, different referents, uncertainty and German/
   English pairings affect alerts?
4. Does INT8 materially differ from **the validated ONNX FP32 baseline** at the
   fixed diagnostic `contradiction >= 0.90` mark?

We must not collapse **textual NLI contradiction** into **authorization for
Memory review**: even a correct textual classification can be irrelevant to
an existing memory slot because subject, time, provenance or modality differs.

## New draft corpus

`scenarios/mvp02c10_memory_conflict_diagnostic.json` contains:

- **94** synthetic pairs (23 draft contradiction, 10 entailment, 61 neutral).
- **11 slices**: same-scope conflict, restatement, independent facts,
  temporal evolution, intention vs outcome, hypothetical/conditional,
  attributed/quoted speech, subject/context mismatch, uncertain modality,
  explicit user correction, and multilingual/cross-language examples.
- An explicit **draft** `expected_nli` label per pair.
- An independently **unverified**, oracle-only `review_applicable` boolean
  with `eligibility_reason`: whether the pair is considered safe to
  *compare for conflict review* given subject, scope, time and assertion
  context. This is **not** an algorithm and it does not grant any rights.
- Directional premise `stored_text` -> hypothesis `candidate_text`.
  Preserve the order and distinguish prior *source evidence* from an
  authoritative fact; these strings are evaluation fixtures, not actual
  persisted Memory records.

**Annotation state: DRAFT_UNREVIEWED.** These labels were newly written and
have **not** been independently adjudicated. Real user interaction samples
were not used. The 94 examples are neither representative nor a blinded
holdout and their precision/recall must not be presented as deployment
reliability. Independent adjudication should cover ambiguous pragmatics and
temporal interpretation before a production candidate decision.

## Offline benchmark

Run on the machine with the existing verified local FineCat ONNX exports.
The benchmark uses the checked-in `persona_agent.local_onnx` adapter,
the same frozen pairs for both variants, `CPUExecutionProvider` and two
inference threads. No model downloads, Ollama, PostgreSQL, or persona
state changes.

```bash
python -m pip install -e ".[onnx-bench,test]"
PYTHONPATH=src python scripts/evaluate_memory_conflicts.py \
  --model-dir "$HOME/models/nli/finecat-nli-v2-xl" \
  --precisions fp32 int8 --threads 2 \
  --warmup 1 --repetitions 2 \
  --report artifacts/mvp02c10-memory-conflict-comparison.json
```

The script preflights exact local model paths, prevents report overwrite, uses
no remote model loading and checks the verified FineCat label order. A
compatible local `onnx/model_quint8_avx2.onnx` (or another compatible
INT8 variant) must already exist. You can run just `--precisions fp32` if
needed. Do not commit reports with real conversation text.

The output contains model and dataset hashes, version/provider, per-case NLI
scores and classification, raw and filtered contradiction marks, slices,
CPU latencies, and FP32-vs-INT8 disagreements.

### What the metrics actually mean

**Gold memory-review candidate:** a draft `expected_nli=contradiction`
**AND** `review_applicable=true`. It is an evaluation label, *not* a
declaration that a persistent memory should be changed.

| Metric | Meaning |
|---|---|
| `nli_argmax_correct` | Three-way textual NLI agreement with draft annotation |
| `raw` | FP/TP/FN/TN from `score.contradiction >= 0.90` against *memory-review* gold, regardless of eligibility |
| `oracle_eligibility_filter` | Hypothetical score if **perfect human-provided applicability** blocked ineligible pairs *before* alerts |
| `by_group` | Error distribution across temporal, modal, provenance and language failure modes |
| `variant_comparison` | Class and fixed-threshold changes from ONNX FP32 to INT8 |

A raw false alert can therefore be either a mistaken NLI score or a
perfectly plausible textual contradiction lacking domain comparability.
**Never report the oracle-filtered numbers as achieved system performance.**
No operational candidate gate exists in this increment.

The 0.90 score is an uncalibrated, **predeclared diagnostic cutoff** retained
from MVP-02C.7. The suite must not be used to tune its replacement and
subsequently claim independent validation.

## Review and decision gate

1. Have independent reviewers annotate the 94 pairs **without looking at
   predictions**, including whether the NLI label and review-applicability
   annotation are defensible. Record disagreement, amendments, and reviewer
   decisions as a *new dataset version* rather than silently editing the
   initial benchmark.
2. Run and upload the FP32 and INT8 report, inspect **every raw false alert**,
   **missed review candidate** and **class/threshold disagreement** by ID.
   Investigate `temporal_change`, `intention_vs_outcome`,
   `explicit_correction` and `multilingual_mixed` first.
3. If the raw-versus-oracle gap is large, implement and test an independent
   subject/temporal/modality candidate gate **before any production NLI
   integration**. The oracle analysis gives a potential upper bound, not a
   validated implementation.
4. Collect a **separate**, source-reviewed and privacy-safe representative
   holdout (not these authored diagnostics) to assess operational precision,
   recall, abstention and FP costs. Only this evidence can support a
   production cutoff or model selection.
5. Evaluate CPU/Ollama contention and background execution cost separately.
   No extra synchronous inference in the ordinary foreground conversation.

### Non-goals and invariants

- No NLI output may edit, supersede, delete or suppress a stored Memory entry.
- A high classifier score alone is neither user confirmation nor sufficient
  evidence for the Memory-domain owner to approve an update.
- No new state owner, background scheduler, network dependency or embedding
  index. No persistent personal data in synthetic reports.
- Existing explicit user correction/approval, source provenance and
  Memory optimistic-version checks remain authoritative.

**Decision:** MVP-02C.10 is an **evaluation instrument**, not completed
model-quality validation. FineCat INT8 remains a provisional advisory-only
candidate until reviewed real-model evidence and separate holdout testing
justify integration.
