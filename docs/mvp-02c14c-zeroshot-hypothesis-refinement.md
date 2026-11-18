# MVP-02C.14C — Zero-Shot Hypothesis Refinement

**Status:** offline-only, paired benchmark, no production inference or Memory changes.

## Why this experiment exists

The MVP-02C.14B CPU evaluation of the existing local
`deberta-v3-base-zeroshot-v2.0` finished with zero transport or schema
failures on 80 author-draft-labeled synthetic examples. Four top-1 dimensions
agreed with draft labels at 65/80 statement type, **16/80 temporal scope**,
**52/80 modality**, and **9/80 pragmatic function**. In particular,
`UNSPECIFIED` frequently outranked a semantically CURRENT self-report,
`UNKNOWN` displaced tentative modality, and `UNKNOWN` dominated the
pragmatic-function candidates. These may be hypothesis design failures,
classifier limitations, draft-rubric problems, or combinations thereof.
The report does **not** support production accuracy claims.

This experiment keeps v14B's original hypotheses unchanged and tests a
*versioned, distinct candidate set*, with no new model dependencies.

## Experiment contracts

`scripts/evaluate_zeroshot_hypotheses.py` performs a **paired inference
comparison on the same model, corpus subset, device, batch size and score
thresholds** in a single process:

1. Execute the entire unmodified v14B reference benchmark on the input
   examples (includes statement type and all four original dimensions).
2. Execute three revised dimensions on exactly those same examples:
   - **Temporal scope:** CURRENT, HABITUAL, PAST_EVENT, PAST_HABITUAL, FUTURE.
     CURRENT refers to a currently valid preference/state, not merely the
     presence of an explicit timestamp. No competing UNSPECIFIED sentence.
   - **Modality:** ASSERTED, TENTATIVE, CONDITIONAL, HYPOTHETICAL.
     No competing UNKNOWN sentence.
   - **Correction signal:** binary CORRECTING / NOT_CORRECTING, separate
     from the prior five-way pragmatic taxonomy. It detects whether the
     text explicitly *claims* a correction, not whether historical Memory
     genuinely needs changing.
3. Compare v14B and refined predictions case by case, with original
   authors' draft labels reserved for scoring only. Do not pass gold,
   case IDs or group names to the model.
4. Preserve strict score validation, no model output parsing into
   Memory claims, non-leaking error diagnostics, and write-once reports.
   Invalid model batches are recorded and cause a nonzero CLI exit code.

**No UNKNOWN or UNSPECIFIED category is imputed as a valid label.**
The current v14C classifier is still a top-1 diagnostic. `--min-score`
and `--min-margin` cause an explicit *abstention*, not an invented
semantic "unknown" class. Defaults 0/0 preserve a transparent uncalibrated
top-1 comparison; there is **no claimed safe threshold**.

For fair paired scoring, old v14B UNKNOWN / UNSPECIFIED *predictions*
are treated as abstentions, and author drafts whose labels are UNKNOWN /
UNSPECIFIED are **excluded from that dimension's eligible denominator**.
Correction gold is CORRECTING if the original pragmatic draft was
CORRECTING, NOT_CORRECTING for explicit INFORMING/REQUESTING/ASKING, and
**unscored** for UNKNOWN. This relabeling is explicitly declared and must
not be mistaken for independent gold adjudication.

The report includes overall and by-group eligible counts, coverage,
conditional agreement, per-label TP/FP/FN, newly-correct and regressed
IDs, batch timing, model load, model/prompt hashes, source SHA and
validation errors. It contains **no user text or raw responses**.

### Run on the existing local model

From repository root, using the same environment as v14B:

```bash
python -m pip install -e ".[zeroshot]"
PYTHONPATH=src python scripts/evaluate_zeroshot_hypotheses.py \
  --suite scenarios/mvp02c13_semantic_scope_corpus.json \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --device cpu --batch-size 4 \
  --report artifacts/mvp02c14c-deberta-paired.json
```

For a **smoke test**, before the full run:

```bash
PYTHONPATH=src python scripts/evaluate_zeroshot_hypotheses.py \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --device cpu --batch-size 2 \
  --case-ids SC001 SC013 SC015 SC022 SC025 SC066 SC075 SC078 \
  --report artifacts/mvp02c14c-deberta-smoke.json
```

Optional **exploratory** abstention thresholds (not calibrated):

```bash
PYTHONPATH=src python scripts/evaluate_zeroshot_hypotheses.py \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --min-score 0.70 --min-margin 0.15 \
  --report artifacts/mvp02c14c-deberta-thresholds.json
```

Reports are write-once. Avoid comparing metrics across runs with differing
source hashes, thresholds or scoring denominators. Compare paired data
within one report first; do not treat legacy generative Memory *scope*
evaluation as equivalent to this classification benchmark.

The full run performs **both** suites (v14B 22 plus v14C 11 natural-language
hypotheses per example, 2,640 NLI premise-hypothesis pairs). Expect longer
wall time than v14B. Model inference is read-only, entirely local via
Transformers; no Ollama, PostgreSQL or network fallback.

## Interpretation / next decision

Determine whether removing competing UNKNOWN/UNSPECIFIED hypotheses and
using a targeted correction signal materially improves temporal,
modality and correction classification **without unacceptable regressions**.
Inspect per-label recall for the sensitive TENTATIVE, CONDITIONAL and
CORRECTING classes; overall accuracy is insufficient.

An improvement on these **previously viewed, synthetic draft examples**
does not prove generalization. Before any selected changes go into the
Conversation Orchestrator, independently annotate v14B data, create an
**unseen holdout**, and evaluate abstention threshold calibration plus
single-message latency. Correcting a statement does not itself authorize
Memory updates. The existing foreground deterministic path and
domain-owned evidence admission remain unchanged.

### Decision register

- Retain existing v14B script and hypotheses as an immutable reference.
- Add v14C as a separate, explicitly opt-in offline comparison.
- No changes to production routing, selected-persona authority,
  Memory extraction, claim provenance, correction or DB schema.
- Model suitability, multi-label time/context interpretation, safe
  thresholds and independent annotation **remain open**.
