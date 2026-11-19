# MVP-02C.14K — Human review completion and guarded model benchmarks

**Status: human annotations NOT yet supplied.** This increment implements
the executable handoff, independent-human-evidence audit, and benchmark gate.
No real independent annotation or reviewed model accuracy is fabricated.
The project cannot complete the external human-review requirement through
automated inference or by generating fake reviewer submissions.

## Evidence and completion state

The merged v14J packet at
`scenarios/mvp02c14j_blind_claim_packet.json` contains **26 newly
written synthetic messages and 33 author-proposed claim spans, with no
semantic labels**. The separate historic v14D author draft labels and v14I
model outputs **must not** be shown to reviewers as an answer key.

`scripts/audit_semantic_review_completion.py status` produces a
source-free, **PENDING_GENUINE_HUMAN_ANNOTATIONS** status report without
creating or guessing human judgments:

```bash
PYTHONPATH=src python scripts/audit_semantic_review_completion.py status \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --report artifacts/14k-private/review-pending.json
```

This only reports the packet's state; it is **not** evidence that a
human has started reviewing.

## Handoff to three genuinely different people

The coordinator must actually assign **two independent human reviewers**
and a **distinct adjudicator**. Obtain their own participant aliases,
not model-generated reviewers. Prepare separate, empty forms:

```bash
PYTHONPATH=src python scripts/audit_semantic_review_completion.py prepare \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --output-dir artifacts/14k-private/forms \
  --reviewer-a-id ACTUAL_REVIEWER_A_ALIAS \
  --reviewer-b-id ACTUAL_REVIEWER_B_ALIAS \
  --adjudicator-id ACTUAL_ADJUDICATOR_ALIAS
```

`prepare` refuses duplicate aliases and existing directories, leaves
all label objects `null` and all attestations `false`, and creates
restricted local files. It does **not** assert humans have been assigned
merely because aliases were typed. Distribute the **same unlabeled packet**
and the corresponding reviewer form to each reviewer **separately**.
The reviewers must not see each other's results, model predictions, or
v14D/v14I results until they have sealed their answers. Each independently
fills every claim's seven semantic fields; the adjudicator reviews both
sealed submissions and gives a rationale for disagreements. Follow the
frozen taxonomy rubric in
`docs/mvp-02c14j-independent-semantic-review-and-comparison.md`.

As per v14J, generate local-only gold *after real completion*:

```bash
PYTHONPATH=src python scripts/review_independent_semantic_labels.py adjudicate \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --reviewer-a artifacts/14k-private/forms/reviewer-a.json \
  --reviewer-b artifacts/14k-private/forms/reviewer-b.json \
  --adjudicator artifacts/14k-private/forms/adjudicator.json \
  --output artifacts/14k-private/adjudicated.json
```

Keep all forms, the adjudicated source-containing fixture, and
benchmarks under **`artifacts/`**, which is Git-ignored. Do not
commit individual annotations or upload them to public GitHub
Actions, and do not use personal conversation logs as a substitute
for these synthetic pilot cases.

## Completion audit (required BEFORE any model call)

`scripts/audit_semantic_review_completion.py audit` reopens
**both reviewer forms, the adjudicator's own completed decisions,
the frozen unlabeled packet, and the resulting gold fixture**:

```bash
PYTHONPATH=src python scripts/audit_semantic_review_completion.py audit \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14k-private/adjudicated.json \
  --reviewer-a artifacts/14k-private/forms/reviewer-a.json \
  --reviewer-b artifacts/14k-private/forms/reviewer-b.json \
  --adjudicator artifacts/14k-private/forms/adjudicator.json \
  --report artifacts/14k-private/review-audit.json
```

The auditor rejects incomplete seven-field annotations,
false/missing attestations, duplicate reviewer/adjudicator identities,
tampered source offsets, incorrect source hashes, changed input forms,
missing rationales for disagreement, altered gold values, and inconsistent
disagreement counts. It reconstructs the complete gold fixture from the
actual submitted forms and **requires exact equality** with the stored
adjudication result. Tests generate simulated forms exclusively to
verify these contracts; **those simulations are not real annotations**.

The write-once **text-free audit** preserves evidence SHA-256 hashes and
per-dimension counts of reviewer agreement/disagreement, separately
counts claims both reviewers resolved, and counts adjudicated
non-unknown and unknown values. Full raw agreement can be inflated by
shared abstentions; the audit therefore also reports agreement among
claims both reviewers resolved. Small-sample rates are descriptive,
not measures of independent annotation reliability or ground-truth
certainty. The report contains **no source text, labels, prompts,
reviewer names or exception bodies**.

Software cannot attest whether reviewers are actual distinct people,
qualified, blind to previous labels, or working independently.
The evidence status remains
`HUMAN_ATTESTATION_NOT_INDEPENDENTLY_VERIFIABLE`.

## Gated real-model benchmark (only after audit)

`run` requires the **original human forms and gold**, rejects missing
or altered review evidence, and performs the audit **before**
constructing the local model client. It then reuses v14J's
existing spaCy lexical, DeBERTa local NLI, or local Ollama/Qwen adapters,
same exact-span evaluation and abstention handling. A report includes
the audit SHA-256, model/threshold provenance, per-repetition
dimension metrics and `NO_MODEL_SELECTED`.

Example local-only Qwen experiment:

```bash
PYTHONPATH=src python scripts/audit_semantic_review_completion.py run \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14k-private/adjudicated.json \
  --reviewer-a artifacts/14k-private/forms/reviewer-a.json \
  --reviewer-b artifacts/14k-private/forms/reviewer-b.json \
  --adjudicator artifacts/14k-private/forms/adjudicator.json \
  --backend ollama --model qwen2.5:7b --repetitions 2 \
  --report artifacts/14k-private/qwen7b-reviewed.json
```

Use the same command with `--backend spacy` or
`--backend deberta --model-dir <installed-local-checkpoint>`
and fresh report filenames. Install `.[test,semantic-eval]` for
spaCy, and `.[zeroshot]` for the existing DeBERTa NLI adapter.
No models are silently downloaded; Ollama connections are loopback-only.
A model need not pass a quality threshold to be recorded as a failed
experiment; invalid predictions count toward errors and abstentions,
**not as incorrect semantic predictions**.

A guarded comparison requires **two or more reviewed model reports**
with the same human-audit checksum, human gold digest, scenario checksum,
and reference eligibility/unknown denominators:

```bash
PYTHONPATH=src python scripts/audit_semantic_review_completion.py compare \
  --packet scenarios/mvp02c14j_blind_claim_packet.json \
  --gold artifacts/14k-private/adjudicated.json \
  --reviewer-a artifacts/14k-private/forms/reviewer-a.json \
  --reviewer-b artifacts/14k-private/forms/reviewer-b.json \
  --adjudicator artifacts/14k-private/forms/adjudicator.json \
  --reports artifacts/14k-private/spacy-reviewed.json \
            artifacts/14k-private/deberta-reviewed.json \
            artifacts/14k-private/qwen7b-reviewed.json \
  --report artifacts/14k-private/paired-reviewed-summary.json
```

Do **not** run this comparative evaluation before sealing genuine
human annotations: exposing predictions can contaminate the blind
review process. Since real annotations are unavailable at the time
of this PR, **no reviewed real-model benchmark has been executed**.

## Limits and next actions

The 33-claim synthetic packet is a **pilot only**. It falls far short
of v14G's **proposed** 200 independently reviewed eligible reference
claims per required semantic dimension. The audit explicitly marks
all under-threshold dimensions and blocks production model selection,
regardless of observed agreement. Even after the pilot is completed,
a real stratified independently reviewed evaluation set is needed
for reliable comparisons, uncertainty estimation, risk-based
negation/temporal/correction analysis, and target hardware p95.
None of this work modifies v2.5.1 foreground routing, domain-owned
Memory, goals, persona emotion or relationship state.

**Decision register**
- **14K-D1:** Human judgments cannot be synthesized by the LLM and
  represented as independent review.
- **14K-D2:** Require source+reviewer forms+adjudication reconstruction
  before any offline reviewed-model benchmark.
- **14K-D3:** Track abstention, agreement, review sample floor and
  provenance independently. Do not silently promote model candidates.
- **14K-D4:** The current external blocker is appointment and completion
  of real human annotation and adjudication.
