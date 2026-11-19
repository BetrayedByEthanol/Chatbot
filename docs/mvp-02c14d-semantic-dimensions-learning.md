# MVP-02C.14D — Semantic Dimensions, Evaluation & Learning-Candidate Contracts

**Status:** offline-only contract/evaluation increment. No Foreground interpretation
routing, DeBERTa/Qwen2.5 training or inference, production Memory admission,
correction, schema migration, background pipeline change or model deployment.

## Architectural review and reuse

Architecture v2.5.1 §§18–20 and the Adaptive Processing companion require
adaptive hybrid inference, source-linked claims, selective historical retrieval,
domain ownership, fast conversation, and optional background cognition.
This increment **reuses** Stage A's conceptual multi-claim design but creates
new versioned offline read models rather than changing the contracts used by
`memory_conflict_eligibility.py`, `memory_conflict_grounding.py`,
`semantic_classification.py`, or `memory_explicit_grammar.py`.

The current `SemanticClaim` combines single-label temporal scope (including
HABITUAL), single-label modality, and pragmatic function. Those are sufficient
for existing narrow deterministic diagnostics but not for representing multiple
claims, asserted conditionals, or state changes independently of corrections.
MVP-02C.14C also revealed provisional annotation errors and poor recall in
important minority classes. More hypothesis tuning would not fix this taxonomy.

## Contracts (pure, no I/O)

- `semantic_dimension_contracts.py` defines `SemanticDimensionsV2`:
  up to eight source-grounded `SemanticDimensionClaim` records, message-level
  `MessageSignal` cues, and claim relations (STATE_CHANGE or CONTRAST).
- Each claim has **independent** past/present/future references (0–3 values),
  habituality, certainty (asserted/tentative/unknown), conditionality,
  hypotheticality, polarity, statement type, and optional source-linked
  subject and temporal/place/condition/attribution qualifiers.
- Empty temporal references and null bools mean **not established**. They
  must not be interpreted as absent, false, or authoritative zero values.
- `CORRECTION_CUE` is only an observable discourse signal. `STATE_CHANGE`
  describes user-reported evolution; it is not a verified correction or
  Memory contradiction. An asserted *conditional* is not an unconditional
  fact; an asserted *intention* is not a completed or guaranteed event.
- `SourceSpan` uses Unicode Python-string [start,end) offsets against the
  complete original turn. `validate_semantic_dimensions` checks source-turn
  identity and that every source-linked quote exists at its exact offset.
  **Lexical grounding is not semantic validation.** A claim may omit a
  qualifier from its primary evidence span; downstream users must inspect
  the entire source turn and qualifiers, not upgrade a partial quote into
  a general current Memory fact.
- The existing `SemanticClaim`/Memory gate are untouched. There is **no**
  automatic v2-to-Memory adapter because no independently validated mapping
  exists yet. Source, selection, privilege and review IDs must come from the
  trusted host, never model-generated JSON.

## Provisional evaluation

`scenarios/mvp02c14d_semantic_drafts.json` contains **15 new synthetic,
author-draft-labeled cases**: historical/current transition, multi-claim,
explicit correction cue without history, state change without correction,
negative preference, tentative assertion, conditional and counterfactual
language, past experience, future intention, recurring activity, and
third-party attribution. `SD005` and `SD003` explicitly avoid promoting
the old SC019/SC071 negative or changing-preference drafts to CORRECTING.

This corpus is a **development diagnostic**, not reviewed ground truth, not
an untouched holdout, and not a basis for deployment quality claims. Keep
`scenarios/mvp02c13_semantic_scope_corpus.json` and v14B/v14C hypotheses
unchanged for historical regression comparison.

`semantic_dimension_evaluation.py` compares injected predictions with
reference cases: exact-span aligned/missed/extra claims; **per-aligned-claim
qualifier coverage** by kind and exact source span (matched/missed/extra,
including repeated qualifiers), both per case and aggregated by kind;
per-dimension eligible, supported, correct, incorrect, abstained,
unscored-reference and unverified prediction counts; temporal-label
TP/FP/FN/TN on known-reference claims; message correction cues and
**directed relation instances** scored separately. Each relation matches on
(kind, relation evidence span, optional source-claim evidence span,
destination-claim evidence span), rather than relation-kind presence or
claim-list indices. Reordering claims without changing which evidence
spans the relation connects is score-neutral; reversing direction, linking
the wrong claim, altering evidence, or duplicating a relation produces
missed/extra counts. Per-case `matched_relations`, `missed_relations`,
`extra_relations`, overall `relation_alignment`, and per-kind
`by_relation` TP/FP/FN/TN preserve these differences without exposing
the source text. Qualifiers on unmatched claims are accounted for through
claim omissions/extras rather than counted twice.
There is no meaningful qualifier true-negative metric because possible
source spans are open-ended. Unknown reference dimensions are **not**
silently converted into negatives. The comparator uses strict exact span
alignment (a conservative extraction diagnostic, not semantic equivalence).
A prediction that keeps SD007's primary claim but drops its external
CONDITION qualifier loses qualifier recall, even if its scalar
conditionality classification remains unchanged.

Use a predictions JSON file shaped as:

```json
{
  "version": "mvp02c14d.predictions.v1",
  "items": [
    {"id": "SD001", "interpretation": {"...": "the complete SemanticDimensionsV2 JSON"}}
  ]
}
```

Run, without model inference:

```bash
PYTHONPATH=src python scripts/evaluate_semantic_dimensions.py \
  --suite scenarios/mvp02c14d_semantic_drafts.json \
  --predictions /path/to/explicit-model-predictions.json \
  --report artifacts/mvp02c14d-paired.json
```

Predictions are **not** derived from gold labels. Missing predictions are
visible abstentions. Reports are write-once, source/prediction hash bound,
and omit source texts and evidence snippets. They do not record model
accuracy absent a real model run and independently reviewed labels.

Before training or model selection: independently adjudicate annotation
rules, add unseen holdout examples, include multi-turn correction contexts,
and measure per-label false positives/false negatives, abstention and actual
single-message latency. Do not claim a classifier detects a correction
**target** based only on a cue such as "actually".

## Learning-candidate contract, not training authorization

`semantic_learning_candidates.py` introduces a scoped,
`(user_id, instance_id)` `SemanticLearningCandidate` with source/review
references, trigger, privacy class and review status. It contains no training
job, model weights, tool authority or new persistence owner. Valid triggers
include user clarification, actual interpretation error, model disagreement,
uncertainty, multi-claim or qualifier risk.

The pure `assess_training_candidate` screen defaults to **not eligible**.
An advisory offline-export decision requires all of:

1. Matching user/persona instance and explicitly authorized source turns.
2. Explicit permission to train on these data and compatible retention.
3. Normal-privacy classification; sensitive/unknown stays blocked.
4. Current taxonomy version and an independently reviewed label.
5. A review ID independently verified by trusted context.

A user correction is **valuable evidence**, but does not automatically label
all the dimensions of the original sentence. Model agreement cannot serve as
independent adjudication. Review IDs, consent and source ownership must be
verified by a trusted host; a schema alone cannot authenticate them.
**Before actual export**, the owning service must recheck database provenance,
source versions, consent/withdrawal, retention/deletion, reviewer identity,
instance isolation, and data minimization. No user conversation is exported
by this increment.

## Background handover integration (deferred)

The existing P4 Background coordinator already runs source-watermarked
SUMMARIZE → EVALUATE → critical updates → ALIGN-01 and only then admits
autonomous work. A future bounded, **optional** learner may obtain candidate
hints from that Evaluation work or passive events, after source/version checks.
Candidate extraction is never a new mandatory readiness barrier; it must not
block critical corrections, P0 conversation, or unrelated authorized P2 work.

Training itself is **separately authorized and periodic**, based on reviewed
sample quality and measured benefit, not triggered on every Foreground-to-
Background transition. Keep private per-instance semantic knowledge in Memory
rather than silently modifying shared model weights. Model selection/deployment
requires independent held-out evaluation, resource budgets, rollback/versioning
and explicit authority.

## Validation

`tests/test_mvp02c14d_semantic_dimensions.py` exercises multi-claim
representation, independent correction/change cues, source offsets and forged
evidence rejection, uncertainty, unknown-vs-false scoring, missing claims,
report write-once/non-leakage, cross-instance isolation, consent, privacy,
retention and independently reviewed annotation gates. All test predictions
are intentionally synthetic; none establishes model performance.

**Decision:** retain old production and evaluation contracts; introduce a
versioned *offline* representation and learning-candidate eligibility screen.
Future fine-tuning is conditional on independently reviewed evidence and
measurable generalization, not a mandate from this PR.
