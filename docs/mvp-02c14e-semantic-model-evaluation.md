# MVP-02C.14E — Semantic model evaluation

**Scope:** offline-only, no route activation, no Memory writes, no model
training and no automatic weight deployment. Architecture v2.5.1 retains
adaptive hybrid processing, Memory ownership, and fast foreground response.

## Review of reusable work

- MVP-02C.14B/14C already supply a **local-only** Transformers zero-shot
  NLI backend with strict score parsing and selectable abstention thresholds.
  Reuse it; do not reimplement the model gateway.
- MVP-02C.14D supplies the versioned multi-dimensional semantic claims,
  source-span validation, an author-draft corpus, and offline structural
  extraction evaluation.
- The first model comparison uses *frozen reference claim spans as inputs*
  to isolate semantic classification. These spans must be passed as
  claim **quotes** with the full source turn, **never** with any draft labels,
  intended scope, qualifiers or relations.

## The three probes

`scripts/evaluate_semantic_models.py` supports:

| Backend | Description | Requirements | Limits |
|---|---|---|---|
| `spacy` | spaCy English tokenization and explicit lexical cues | Optional `.[semantic-eval]` | **Structural cue baseline only**, deliberately abstains on unsupported properties. |
| `deberta` | Local zero-shot NLI on independent paired hypotheses | Existing `.[zeroshot]`, local native HF model directory | No downloaded weights, no implicit calibration, 9 hypothesis groups per claim, slower than rules. |
| `ollama` | Local Ollama chat with bounded JSON response and strict label validation | Running local Ollama and explicitly chosen installed model | No Internet service fallback; bounded timeout, schema errors become abstentions. |

All three use the same ordered list of dimensions: statement type,
certainty, polarity, habituality, conditionality, hypotheticality, and
past/present/future references. Temporal cues are **multi-label**. An empty
temporal prediction means abstention, not an assertion of three negatives.
A partially unresolved NLI temporal response abstains on the temporal set.

Example commands from repository root:

```bash
# No model download; optional spaCy tokenizer baseline
python -m pip install -e ".[test,semantic-eval]"
PYTHONPATH=src python scripts/evaluate_semantic_models.py \
    --backend spacy --report artifacts/mvp02c14e-spacy.json

# Real native DeBERTa NLI model already installed on local disk:
python -m pip install -e ".[test,zeroshot]"
PYTHONPATH=src python scripts/evaluate_semantic_models.py \
    --backend deberta --model-dir /models/deberta-v3-nli \
    --case-ids SD001 SD002 SD007 SD009 \
    --min-score 0.65 --min-margin 0.10 \
    --report artifacts/mvp02c14e-deberta-smoke.json

# Real installed Qwen2.5 served locally through Ollama; model tag is explicit:
PYTHONPATH=src python scripts/evaluate_semantic_models.py \
    --backend ollama --model qwen2.5:7b \
    --case-ids SD001 SD002 SD007 SD009 \
    --report artifacts/mvp02c14e-qwen-smoke.json
```

Do **not** compare a four-case subset with an entire suite. For a controlled
paired comparison, run **the same case IDs**, local hardware, model versions,
batch settings and thresholds. The comparison CLI rejects different source
suite hashes, case selections, reference claim counts, dimension sets or
duplicate backend identities:

```bash
PYTHONPATH=src python scripts/compare_semantic_model_reports.py \
    --reports artifacts/mvp02c14e-spacy.json artifacts/mvp02c14e-deberta.json \
              artifacts/mvp02c14e-qwen.json \
    --output artifacts/mvp02c14e-paired-comparison.json
```

The comparison reports **draft-label agreement**, both conditional on
supported predictions and over all eligible reference labels, plus coverage
and abstentions. They are not estimates of unbiased real-world accuracy.
 Model loading is not included in per-claim
latency: measure separately for deployment. The report includes exact suite
content hash, prompt version, model backend identity, sample claim counts,
per-dimension coverage/abstentions and draft-label agreement, temporal
per-label TP/FP/FN/TN with abstentions, errors and inference timings.

## Valid inference boundary

The backend sees **original source + reference-provided claim text**, not
reference classification labels. The probe deliberately does not use
`evaluate_dimension_pairs` to score full claims because doing so would
artificially grant the classifier perfect claim extraction, qualifier
preservation, and relations through oracle copying. Reports explicitly
label `ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY`, contain **no source
text or individual model responses**, and cannot justify Memory admission.

All 15 MVP-02C.14D examples have synthetic, author-written provisional
labels. **This is not an independent gold set** and has already been used
during architecture and rule design; any agreement is development-only,
not unbiased model-quality evidence. Report actual measurements only after
running real local models; tests inject synthetic backends and never count as
model accuracy.

## Acceptance before considering production routing

1. Independently adjudicate a versioned holdout, resolve conflicting
   statement/time/correction annotation rules, and add multi-turn context.
2. Assess source-independent extraction separately: primary claim recall,
   qualifier coverage and directed relation correctness (v14D evaluator).
3. Run paired real-model comparisons using the same reviewed cases, including
   false-positive semantic memory eligibility and high-risk corrections.
4. Measure latency (cold load, steady-state single claim and compound claim),
   resource contention, optional abstention, retries, and fallback behavior.
5. Require explicit signoff for any new runtime provider and unchanged
   domain-owned Memory/correction gates.

## Adjacent P2 follow-up

The v14D `by_temporal_label` scorer previously converted empty predicted
temporal labels into three negatives. This increment fixes that defect:
`abstained` and `unscored_reference` remain separate from TP/FP/FN/TN.
Its regression tests are included in the existing v14D test module.

**Decision:** retain v14D offline contracts, add optional local model
classification probes only, and defer training, model selection and
integration until independent evidence shows a measurable benefit.
