# MVP-02C.14J — Independent semantic labels and local model comparison

**Status: contract-first review workflow implemented; human annotation is
PENDING.** There are **no independently adjudicated semantic reference labels
yet**. The PR does not invent reviewer identities, judgments or an accuracy
score. The v2.5.1 persona inference/Memory contracts remain unchanged.

## Motivation and existing evidence

MVP-02C.14I's real Qwen2.5:0.5B experiments had valid structured
outputs but poor agreement against 15 **author-draft** cases, and
prompt tuning was a negative result. The existing
`scenarios/mvp02c13_blind_review_packet.json` has 80 historical,
previously explored unreviewed semantic-scope texts; it is **not**
a fresh independently reviewed holdout. The previous v14D corpus has
15 cases/18 reference claims with labels and cannot be converted to
independent gold by automatically re-labeling.

This increment introduces a **new, label-free pilot packet** and
human-only review/adjudication contracts plus tools to compare
real **spaCy lexical, local DeBERTa and local Ollama/Qwen** against
the same reviewed claim labels, once human annotations are supplied.

## 1. New blind packet

`scenarios/mvp02c14j_blind_claim_packet.json` contains **26 new
synthetic cases, 33 source-grounded target claim spans**, and **NO
semantic reference labels**. It varies persons, assertions,
tentativeness, negation, preferences, historical versus present
states, future intentions, habitual actions, quotation/attribution,
conditionals, counterfactuals and multiple claims.

This is **not** a representative production sample, and novel
synthetic text does not guarantee lack of foundation-model pretraining
exposure. **The target claim boundaries are author-selected**:
the evaluation is conditioned on oracle spans and cannot measure
claim extraction, qualifier extraction, relation inference, correction
targeting or Memory admission.

Do **not** benchmark candidate prompts against this unlabeled packet
or reveal predictions to reviewers before the reference labels are
sealed. The prior experimental prompt has not been promoted.

## 2. Human review protocol

Two **different human reviewers** separately receive the same
label-free packet. They must complete **all seven v14E dimensions**
for **each target claim**, including explicit `UNKNOWN`, `null`
and empty temporal sets where evidence is insufficient. They may
consult the full sentence, not reference labels, model outputs,
historical model errors or other reviewers' answers.

A third **distinct human adjudicator** reviews the two completed
submissions after each is sealed, resolves all items independently,
and supplies a rationale for every disagreement. All three sign
specific attestations. The code checks completion, types, source
checksums, stable claim order, distinct supplied actor identifiers,
exact enum values, and disagreement rationale.

**Important limit:** software cannot verify the real-world identity,
independence or qualifications of reviewers, or whether they actually
avoided previous predictions. Outputs are marked
`HUMAN_REVIEW_ATTESTED_NOT_INDEPENDENTLY_VERIFIED`, **not**
`INDEPENDENTLY_VERIFIED_GOLD`. Review coordination and actual
human judgments remain external prerequisites.

Run from repository root with a Python environment installed using
`python -m pip install -e ".[test]"`, and `PYTHONPATH=src`:

```bash
mkdir -p artifacts/14j-private
PYTHONPATH=src python scripts/review_independent_semantic_labels.py form \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --role reviewer --actor-id human-reviewer-a \
  --output artifacts/14j-private/reviewer-a.json
PYTHONPATH=src python scripts/review_independent_semantic_labels.py form \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --role reviewer --actor-id human-reviewer-b \
  --output artifacts/14j-private/reviewer-b.json
PYTHONPATH=src python scripts/review_independent_semantic_labels.py form \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --role adjudicator --actor-id human-adjudicator \
  --output artifacts/14j-private/adjudicator.json
```

Each blank `items` entry contains `case_id`, `claim_index`,
`labels: null`, and `rationale: ""`. Humans independently fill
`labels` with **exactly**:
`statement_type`, `certainty`, `polarity`, `habituality`,
`conditionality`, `hypotheticality`, `temporal_references`.
The allowed enum and nullable values are enforced by
`ProbeLabels.from_mapping`. Reviewers set their own attestation
booleans to `true` **only if the corresponding statements are true**.
The adjudicator resolves conflicting submissions with an explanatory
rationale and completes every label (not a blind majority vote).

Then generate the **source-containing, local-only** adjudicated fixture:

```bash
PYTHONPATH=src python scripts/review_independent_semantic_labels.py adjudicate \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --reviewer-a artifacts/14j-private/reviewer-a.json \
  --reviewer-b artifacts/14j-private/reviewer-b.json \
  --adjudicator artifacts/14j-private/adjudicator.json \
  --output artifacts/14j-private/adjudicated.json
```

All forms and adjudicated source+labels MUST remain under
`artifacts/`, which is already Git-ignored. **Do not commit them,
upload them as public CI artifacts, or paste raw private annotations
into model prompts.** The adjudicated fixture records reviewer
submission SHA-256 hashes, actor IDs and per-dimension disagreement
counts; the review process itself remains human-attested.

## 3. Side-by-side model comparison (requires human-reviewed fixture)

`scripts/compare_independent_semantic_models.py` refuses unlabeled
or draft reference suites. It validates every reviewed source/claim
span **against the immutable blind packet and its SHA-256**, and
checks human-attestation provenance before a model sees source text.
Model adapters see **only full source and target quote**, never
the reference labels.

Use a real spaCy tokenizer baseline, a locally installed DeBERTa
checkpoint with tokenizer+weights, and an explicitly local Qwen
Ollama tag. Example commands, **not yet executed** because
human review is pending:

```bash
PYTHONPATH=src python scripts/compare_independent_semantic_models.py run \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14j-private/adjudicated.json \
  --backend spacy --report artifacts/14j-private/spacy-metrics.json

PYTHONPATH=src python scripts/compare_independent_semantic_models.py run \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14j-private/adjudicated.json \
  --backend deberta --model-dir /path/to/installed-local-nli-checkpoint \
  --report artifacts/14j-private/deberta-metrics.json

PYTHONPATH=src python scripts/compare_independent_semantic_models.py run \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14j-private/adjudicated.json \
  --backend ollama --model qwen2.5:7b \
  --ollama-url http://127.0.0.1:11434 \
  --report artifacts/14j-private/qwen7b-metrics.json

PYTHONPATH=src python scripts/compare_independent_semantic_models.py compare \
  --reports artifacts/14j-private/spacy-metrics.json \
            artifacts/14j-private/deberta-metrics.json \
            artifacts/14j-private/qwen7b-metrics.json \
  --report artifacts/14j-private/paired-summary.json
```

Qwen runs with explicit JSON Schema to avoid v14H's invalid JSON
value issue; DeBERTa uses its existing NLI abstention cutoffs.
No model is downloaded automatically; DeBERTa requires local
valid native weights and tokenizer. Ollama is loopback-only
with no environment HTTP proxy. A model's unavailability is
not silently replaced with a synthetic response.

Each backend records local fingerprint, active NLI thresholds,
environment, per-repetition errors, abstentions, support,
conditional agreement, and reviewed-label agreement on
**all eligible reference claims**. Model exceptions become
abstentions, **not incorrect semantic predictions**.
Paired comparison verifies identical source/reference hashes,
cases and per-dimension eligible/unscored-reference denominators
for **every repetition of every model**. Outputs omit source
text, predictions and raw exception messages; they say
`NO_PRODUCTION_MODEL_SELECTED` even when results look promising.
The labels are **human-process-attested**, not verified by code.

## 4. Remaining evaluation work

This 33-claim packet is a **pilot for annotation and scoring contracts**,
far below the v14G **proposed** 200 independently reviewed
eligible examples per required dimension. It is imbalanced and
does not provide a sound deployment confidence interval. Further
work after real adjudication includes class-stratified coverage
(including negative and unknown reference labels), claim-level
disagreements, repeatability, error costs, real extraction with
qualifiers and relations, correction safety, and target hardware
p95/CPU/RAM measurements.

**Pending external action:** Recruit/assign at least two genuinely
independent human reviewers and a separate human adjudicator to
complete forms; obtain real intended DeBERTa and Qwen checkpoints.
Do not claim independent gold or comparative model accuracy
until those steps are actually completed.

### Decision register

- **14J-D1:** No synthetic labels generated by the assistant or models
  may masquerade as independently reviewed gold.
- **14J-D2:** Human review is explicitly attested and checksummed but
  cannot be independently validated by software; provenance is
  preserved without overstating certainty.
- **14J-D3:** Probe comparisons are offline, reference-span-conditioned,
  advisory and non-authorizing; no changes to foreground semantic routing,
  persona or Memory state.
