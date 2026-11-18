# MVP-02C.13 — Semantic Scope Extraction Evaluation

**Scope:** Offline, reproducible evaluation of *actual* source-grounded
interpretation using the existing deterministic Stage A classifier and
Memory's bounded explicit-claim parser. No new extraction model, Memory
write, PostgreSQL query, NLI threshold selection, production routing,
background task, or persistence migration.

**Current evidence grade:** **AUTHOR-LABELED SYNTHETIC DIAGNOSTIC ONLY.**
No independent human annotator has reviewed these examples, and the
candidate rules were known when this set was authored. Results must **not**
be called independently verified precision, recall, production accuracy
or calibration. The independent-review workflow is implemented, but its
completion is not asserted.

## Why this evaluation is separate from MVP-02C.11 and .12

- **02C.11:** pure domain metadata evaluator; 21 hand-structured, gold-aware
  fixtures; no natural-language extraction was measured.
- **02C.12:** authenticated PostgreSQL turn + admitted Memory provenance
  integration. Only a *very narrow*, safe, deterministic current enjoyment
  grammar is supported. An admitted quote substring cannot override
  qualifiers in the **complete original user turn**.
- **02C.13:** checks the actual Stage A output and a full-turn deterministic
  scope adapter against labeled synthetic text, including **unsupported**
  wording, time qualifiers, entity switches, tentative modalities, quoted
  claims, explicit corrections and multi-claim turns. It reports *how
  often the existing implementation abstains* rather than copying oracle
  scope metadata into the prediction.

## Dataset and annotation contract

`scenarios/mvp02c13_semantic_scope_corpus.json` contains **80**
new synthetic examples in **nine** categories:

| Group | Cases | Focus |
|---|---:|---|
| `baseline_rules` | 14 | Existing deterministic supported and partial rules |
| `paraphrase_gap` | 10 | Legitimate current preferences outside baseline phrasing |
| `temporal_context` | 11 | Seasonal, weekend, place, historical and qualified assertions |
| `tentative_uncertain` | 8 | Tentative vs asserted, uncertainty about present facts |
| `intent_conditional` | 8 | Intentions, conditionals and counterfactuals |
| `attribution_entity` | 8 | Brother/friend/pet identity, quote provenance |
| `compound_multiple` | 6 | Multiple clauses, conflicting qualifiers, comparisons |
| `explicit_correction` | 7 | Corrections expressed with and without supported grammar |
| `other_non_slot` | 8 | Questions, requests, non-memory and other predicates |

Each draft record gives an entire source sentence and:
`statement_type`, `temporal_scope`, `modality`,
`pragmatic_function`, `subject_key`, `predicate_key`,
`context_key`, `normalized_value` and
`narrow_current_scope`. **Null** identifies a dimension not established
by the source; `UNKNOWN` is used only for supported semantic enums.
No provenance, admission status, authority or Memory ID is guessed.

The last label is a **specific, evaluation-only target**: *the entire turn
unambiguously asserts the user's current general enjoyment/dislike for one
of gardening, reading or walking, without condition, quoted attribution,
context/time restriction, uncertainty, or a multi-claim ambiguity.* Exactly
**28/80** author draft cases satisfy it. This is **not**
`memory_conflict_eligible` (which also requires an actual admitted Memory
record and source/owner validation). The context key `general` is only
emitted by the narrow full-turn grammar, not guessed for arbitrary text.

The file is immutable diagnostic v1 unless an explicit version bump is
made; its SHA256 is recorded in every generated report/review template.

## Evaluation interface

`scripts/evaluate_semantic_scope.py` loads strict, bounded JSON and calls:

1. `RuleSemanticClassifier.classify(text, source_turn_id=...)`.
2. `validate_semantic_evidence` against the **entire** supplied source.
3. The existing `parse_explicit_claim(text)`, also against the **entire**
   source, to build canonical user/predicate/value metadata.
4. Conservative alignment: one claim, full quote, `PREFERENCE`,
   `CURRENT`, `ASSERTED`, informing/correcting, topic agreement and
   allowlisted canonical activity. Otherwise report partial semantics
   or abstention—never fabricate a narrower scope.
5. Measure: Stage A interpretation coverage; full narrow-scope emission;
   draft-labeled true/false activations and missed supported scopes;
   dimension-level agreement **only where that dimension was predicted
   non-null**; category-level abstention; case-level mistakes. A null
   prediction matched to null draft does **not** earn a correct prediction
   (e.g., SC013 has no extracted `normalized_value`). For every dimension,
   `correct + incorrect == supported` and
   `supported + abstained == total`.

A valid current preference may **fail to be extracted** because its
paraphrase is unrecognized. That is counted as missed coverage, not scored
as success just because it was safely abstained. Conversely, predicting
a canonical current-general scope for a qualified/foreign/conditional
statement counts as a false activation (safety regression).

```bash
PYTHONPATH=src python scripts/evaluate_semantic_scope.py \
  --suite scenarios/mvp02c13_semantic_scope_corpus.json \
  --report artifacts/mvp02c13-baseline-semantic-scope.json
```

The result contains **no verbatim text**, full source IDs, or private user
records. It exposes example ID, group, predicted label comparison and
reason. Reports are write-once (no overwrite), with source SHA and
draft annotation status, and do not contain a production Memory command.

## CI dependency isolation and parser ownership

The first PR #71 CI run failed specifically on **“Verify contract tests
without Psycopg”**; the PostgreSQL-backed test step passed. The offline
runner imported `memory_extraction.py`, which imports Psycopg even for its
pure, deterministic `parse_explicit_claim` helper. This violated the
existing no-database-driver contract test.

The existing bounded positive-allowlist grammar was moved **without changing
its matching semantics** to
`src/persona_agent/memory_explicit_grammar.py`, a dependency-light module.
The Memory owner `memory_extraction.py` still imports and re-exports
`ExplicitClaim`, `parse_explicit_claim`, `_slug`,
`_supported_safe_claim`, `_BLOCKED` and `_SAFE_HOBBIES` for existing callers,
including `memory_model_extraction.py`. The offline evaluation script
imports the same parser directly, avoiding Psycopg and retaining a single
implementation rather than copying the whitelist or inventing new semantics.

Regression tests assert the SC013 null/null case is not credited as a
prediction, require every dimension's supported/correct/incorrect/abstained
counts to reconcile, and exercise the parser under an import guard that
rejects Psycopg. No Memory persistence, extraction admission policy or
event handler behavior was intentionally changed.

## Independent, blinded annotation workflow

Two real, independent annotators are required **before any approved gold
dataset or general extraction performance claim**. This PR cannot create
or impersonate independent human review.

1. Use the unlabeled `scenarios/mvp02c13_blind_review_packet.json`
   containing only **ID + full text**, without diagnostic group tags,
   gold, proposed labels or model output. Reviewers should *not* read
   the author-labeled source file or the evaluation results.
2. For each reviewer, generate a SHA-bound editable annotation template
   and send separate copies:

   ```bash
   PYTHONPATH=src python scripts/evaluate_semantic_scope.py \
     --blind-template \
     --suite scenarios/mvp02c13_semantic_scope_corpus.json \
     --report artifacts/mvp02c13-reviewer-a.json
   ```

   Repeat with a different output filename for reviewer B. Each form
   contains the same **ID + text** plus empty field labels. They fill
   **every** field and assign a distinct `reviewer_id`. No results,
   labels, category hints or expected outcome are embedded. Reviewers
   use the same semantic vocabulary and the `narrow_current_scope` rubric
   above, with `null` / `UNKNOWN` for genuinely unresolved dimensions.
3. Compare the completed forms without implicitly approving agreements:

   ```bash
   PYTHONPATH=src python scripts/evaluate_semantic_scope.py \
     --suite scenarios/mvp02c13_semantic_scope_corpus.json \
     --review-a artifacts/mvp02c13-reviewer-a.json \
     --review-b artifacts/mvp02c13-reviewer-b.json \
     --report artifacts/mvp02c13-review-disagreements.json
   ```

   The comparison verifies source SHA, complete ID coverage, unmodified
   source text, valid complete label schema and distinct reviewer IDs.
   It lists disagreements per dimension and retains status
   `REQUIRES_INDEPENDENT_ADJUDICATION`. Two distinct IDs cannot *prove*
   independence. Even unanimous records need human adjudication and a
   **new versioned gold dataset** before adoption.
4. Rerun the baseline on that separately versioned adjudicated set, then
   evaluate a separate untouched holdout before claiming model quality.
   Do not tune deterministic rules or selection on the same dataset that
   is used to report final accuracy.

## Scope and remaining decisions

- No semantic model is enabled on every user turn; this script is entirely
  offline. **Sparse signal routing / cost-aware processing in the APS
  companion remains a proposal**, not a confirmed change to architecture
  v2.5.1. Do not introduce another mandatory classifier or per-message
  Memory sweep on the strength of an offline score.
- The current corpus is intentionally focused on narrow current preference
  claims. Generic `subject_key`, `predicate_key`, location, pet entity,
  temporal validity intervals and contextual semantics still require a
  reviewed scope extraction proposal/adapter and evidence-linked validation.
- The eventual production path must keep selected `(user_id, persona_id)`
  scope, authenticated source turns, owner policies, version fencing,
  privacy and allowed correction actions. No LLM inference or external
  tool call inside a PostgreSQL transaction.
- Human adjudication, privacy-safe naturalistic holdout, error review and
  latency/cost measurement remain outstanding. This increment is an
  **evaluation harness and reviewed-data intake contract**, not an
  eligibility classifier deployment.

**Decision register:** no new architectural authority; dataset labels
provisional; offline evaluation available; independent labeling not
performed; production Memory scope integration unchanged.
