# MVP-02C.14B — Local DeBERTa zero-shot semantic classification evaluation

**Status: offline experimental benchmark; no changes to production processing.**

## Goal and design boundary

Evaluate whether the **already installed local**
`~/models/deberta-v3-base-zeroshot-v2.0/` is a useful low-cost
classifier for bounded semantic dimensions before modifying the persona's
adaptive processing pipeline. Reuse the **unchanged**, 80-example author-labeled
synthetic corpus `scenarios/mvp02c13_semantic_scope_corpus.json` and the
existing deterministic Stage A as the comparison baseline.

This script does **not** perform subject/predicate identification, claim
normalization, contradiction detection, canonical Memory scope derivation,
Memory admission, correction execution, database operations, events or user
conversation processing. Zero-shot scores are advisory only. The purpose is
to evaluate classification, not to promote semantic labels into source truth.

## Classification contract

`scripts/evaluate_zeroshot_semantics.py` defines **frozen, versioned**
natural-language hypotheses, separately for:

| Dimension | Candidate labels |
| --- | --- |
| `statement_type` | FACT, PREFERENCE, EXPERIENCE, INTENTION, OPINION, UNKNOWN |
| `temporal_scope` | CURRENT, HABITUAL, PAST_EVENT, PAST_HABITUAL, FUTURE, UNSPECIFIED |
| `modality` | ASSERTED, TENTATIVE, CONDITIONAL, HYPOTHETICAL, UNKNOWN |
| `pragmatic_function` | INFORMING, REQUESTING, CORRECTING, ASKING, UNKNOWN |

The native Hugging Face zero-shot pipeline performs independent
entailment/contradiction inference (`multi_label=True`,
`hypothesis_template="{}"`); its hypothesis descriptions are complete
sentences. One ranked top label per dimension is reported **for
diagnostics only**. The complete unnormalized score map is also reported
so we can investigate multiple plausible labels, uncertainty and
compound utterances. Scores are **not** calibrated probabilities and
cannot be compared across model families as calibrated confidence.

Optional `--min-score` and `--min-margin` controls allow evaluating
**candidate** abstention policies. Their defaults are **0.0** (top-1
diagnostic, no threshold-based abstention). These experimental thresholds
do not authorize decisions about Memory or user facts. No attempt is
made to infer missing schema fields from a winning label.

Four dimensions are measured independently; a correction may also be
a preference, and a sentence may describe both past and present time.
A single top label is therefore a limited diagnostic view rather
than a complete semantic representation.

## Running locally

The model directory already contains native `model.safetensors` and
tokenizer files; the script **does not download any weights** or use
Ollama. Only install the optional Python dependencies:

```bash
python -m pip install -e ".[zeroshot]"
```

Run the unchanged 80-case corpus:

```bash
PYTHONPATH=src python scripts/evaluate_zeroshot_semantics.py \
  --suite scenarios/mvp02c13_semantic_scope_corpus.json \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --device cpu --batch-size 4 \
  --report artifacts/mvp02c14b-deberta-semantic.json
```

Change to `--device cuda:0` if a supported CUDA-enabled PyTorch install
and GPU are available. A basic smoke test requiring fewer model calls:

```bash
PYTHONPATH=src python scripts/evaluate_zeroshot_semantics.py \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --device cpu --batch-size 2 \
  --case-ids SC001 SC013 SC015 SC022 SC025 SC036 SC066 SC075 \
  --report artifacts/mvp02c14b-deberta-smoke.json
```

To explore (not preapprove) abstentions:

```bash
PYTHONPATH=src python scripts/evaluate_zeroshot_semantics.py \
  --model-dir "$HOME/models/deberta-v3-base-zeroshot-v2.0" \
  --min-score 0.7 --min-margin 0.15 \
  --report artifacts/mvp02c14b-deberta-thresholds.json
```

The full 80 examples × 22 hypotheses = **1,760 NLI premise/hypothesis
pairs**. Performance depends on CPU/GPU and batching; measure before
considering any interactive path.

The dataset and its `sha256` are read and verified before model load.
Model and tokenizer are loaded from the requested **local directory only**
with Hugging Face offline environment flags; no network fallback.
Reports are **write-once**. Run with different names per configuration.
Any batch failure is explicit in the resulting report and yields a
nonzero CLI exit code (the JSON report is still saved for diagnosis).

## Metrics and comparison

A versioned JSON report includes:

- Source dataset digest and provisional author-label status; selected
  model directory name, device and thresholds; hypothesis version/digest.
- For each dimension, Stage A versus zero-shot **supported, correct,
  incorrect, abstained, errors, conditional agreement and coverage**.
  Null predictions matching null drafts **never** count as correct.
- Breakdowns by the existing diagnostic groups (including
  `paraphrase_gap`, `explicit_correction`, `temporal_context`,
  `tentative_uncertain` and `attribution_entity`).
- Case IDs, per-dimension top labels, full NLI score maps, abstention
  and validation status, batch timings, wall-time model load, total
  inference time and approximate classified dimensions/second.
- No example source text, raw model output, unredacted exception messages,
  file-system model path or user data in the report.

**Critical comparison limitation:** the earlier Qwen and Llama reports
measure **canonical narrow-scope extraction**; this benchmark measures
only **four classification dimensions**. Compare those dimensions on the
same corpus, but do not infer that zero-shot classification achieves
Memory extraction recall, correction safety, or the same latency
measurement as a generative pipeline.

**Annotation limitation:** all 80 labels remain synthetic, provisional
author drafts, not independently adjudicated. Neither repeated tests
on this dataset nor a high conditional agreement prove production
accuracy. Independent blind annotation, an untouched holdout, and
threshold calibration are mandatory before any production use.

## Test strategy

`tests/test_mvp02c14b_zeroshot_semantics.py` uses a scripted
(fake) NLI backend to verify reverse-ordered HF labels, completeness
of the four dimensions, source/draft isolation, abstention accounting,
batch errors, no optional Transformers/Torch/Psycopg imports, and
write-once reporting. CI does **not** load a model or claim actual
classification accuracy. Running a benchmark using the installed
weights on the model host is a separate manual task.

### Decision register

- **Confirmed for this increment:** offline evaluation only; existing
  v2.13 corpus unchanged; native local DeBERTa model accepted as an
  opt-in experiment; no production routing or Memory writes.
- **Open:** measured zero-shot coverage and latency, model comparison,
  whether a multi-label temporal representation is needed, calibration,
  independent annotation and holdout validation.
