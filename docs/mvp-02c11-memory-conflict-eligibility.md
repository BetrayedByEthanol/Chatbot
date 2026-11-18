# MVP-02C.11 — Memory Conflict Eligibility Evaluation

**Scope:** A small **pure, typed, read-only** metadata eligibility evaluator,
plus reproducible offline policy fixtures. No database queries, no model
inference, no new state owner, no changed production routing or permissions.

**Architecture:** Persistent Persona Agent v2.5.1; existing
`semantic_contracts.py`, `memory_contracts.py`, `memory.py`, and
`memory_extraction.py` remain authoritative for their domains. This is
an isolated **contract-first** increment to prepare safe integration, not
an NLI deployment.

## Why

In the user's v2 FineCat report (dataset SHA256
`46310133cd3909b7e73b310478e2fed3f0f54d54018e1cb6e56f4118ca23b462`):

| Metric on **94 provisional synthetic pairs** | ONNX FP32 | ONNX INT8 |
|---|---:|---:|
| Unfiltered contradiction false alerts at 0.90 | 3 | 2 |
| Raw true/false negative | 17 TP / 6 FN | 17 TP / 6 FN |
| Explicit user corrections passing the 0.90 mark | 3 / 8 | 3 / 8 |
| Median latency | 218.7 ms | 141.7 ms |

The previous *oracle* eligibility filter removed false alerts, but it
consumed perfect synthetic gold annotations. It was **not an implemented
classifier**. This increment supplies a deterministic decision contract over
structured input, **not** an implemented general-language scope/temporal
extractor, authenticated-memory adapter, or verified end-to-end solution.

## Typed contract

`src/persona_agent/memory_conflict_eligibility.py` defines:

- `ClaimScope`: canonical subject/predicate/context identity, temporal
  category (reusing Stage A), optional paired **timezone-aware** validity
  interval. Context is required and must be grounded upstream; do not
  fabricate `general` when unknown.
- `PriorClaimSnapshot`: authenticated-context identifiers, source turn,
  Memory kind, scope and one of `ADMITTED_ASSERTED`,
  `TENTATIVE_UNADMITTED`, `OTHER_UNADMITTED`.
  Admitted snapshots **require** a memory ID and version; unadmitted sources
  **cannot** carry Memory identity. These are read-only *input claims*:
  passing this schema does **not** authenticate or prove the database record.
- `NewClaim`: source-linked candidate with canonical scope, Memory kind
  and the already-defined Stage A statement/modality/pragmatic labels.
  The caller must perform `validate_semantic_evidence` and independently
  derive canonical keys from trusted context. This module cannot establish
  provenance merely from a caller-supplied UUID.
- `evaluate_conflict_eligibility(prior, new) -> EligibilityDecision`:
  pure deterministic evaluation, no textual NLI scores or 0.90 threshold.
  Every result has a decision, a reason, `scope_compatible` (true/false/
  unknown), and `nli_allowed`.

| Disposition | Meaning / caller responsibility |
|---|---|
| `NLI_CANDIDATE` | Two admitted/current asserted claims are *comparable*. NLI may be used as **advisory** evidence. Compatible restatements can also reach this path; no conflict has been established. |
| `CORRECTION_PATH` | An asserted explicit correction is connected to an admitted, same-scope record. Route to **existing** Memory owner correction validation, independent of NLI. No authorization or mutation was issued. |
| `TENTATIVE_RESOLUTION` | A definite claim may update prior tentative conversational understanding, not Memory. Further conversation/cognition owner checks required. |
| `SKIP` | Known different scope/subject/predicate, disjoint time, unadmitted source, new tentative/conditional/hypothetical claim, or inappropriate act. |
| `ABSTAIN` | Missing or ambiguous time/semantic interpretation. Do not guess a conflict or lose the original evidence. |

### Comparison logic

```text
Validated Memory/Conversation snapshot + grounded new claim
  -> same user/selected persona identity? [otherwise SKIP]
  -> same Memory kind, canonical subject/predicate/context?
  -> time: disjoint? SKIP; unresolved? ABSTAIN
  -> new statement: tentative/hypothetical/nonassertion? SKIP
  -> prior tentative? TENTATIVE_RESOLUTION (proposal)
  -> prior otherwise unadmitted? SKIP
  -> asserted explicit correction? CORRECTION_PATH
  -> asserted comparable claim? NLI_CANDIDATE
```

**Temporal scope is a *fact-validity period*, not the literal date appearing
as the value.** For example: the two statements *"I think my appointment
is Friday"* / *"My appointment is Thursday"* concern the **same currently
believed appointment** whose **day value** differs. They are not two
independent historical facts to compare by Friday vs Thursday intervals.

An unbounded historical event is **not** automatically considered the same
event as another historical event, and unspecified timing abstains. Broad
CURRENT/HABITUAL values may be *compared* as an active slot but this does
not prove simultaneous truth or authorize replacement.

## Test coverage

`tests/test_mvp02c11_memory_conflict_eligibility.py` checks:

- Same-scope candidates including entailments; explicit corrections
  without any 0.90 NLI requirement.
- Separate reference/non-reference source admission; new tentative,
  hypothetical, conditional, unknown and intention claims.
- Different user/persona, subject, predicate, context (e.g. pet dog/cat),
  Memory kind; temporal disjointness, overlapping precise windows,
  partially unknown dates, malformed/unaware timestamps.
- Snapshot metadata cannot include a Memory ID for an unadmitted source;
  invalid decision combinations are rejected by schema.
- **21 selected** v2 example IDs with **hand-supplied structured** metadata
  and declared expected dispositions.

`scenarios/mvp02c11_structured_eligibility_fixtures.json` is a
source-hash-linked, **synthetic, gold-aware** metadata fixture, not an
unseen holdout or output from an automated semantic extractor.

Run its offline contract check without models or PostgreSQL:

```bash
PYTHONPATH=src python scripts/evaluate_memory_conflict_eligibility.py \
  --suite scenarios/mvp02c10_memory_conflict_diagnostic_v2.json \
  --fixtures scenarios/mvp02c11_structured_eligibility_fixtures.json \
  --report artifacts/mvp02c11-structured-eligibility.json
```

It checks the source dataset hash, writes a **new** report, compares the
pure evaluator's selected dispositions and scope/eligibility against the
provisional v2 oracle, and exposes per-case disagreements. Agreement here
can only demonstrate **policy-contract consistency on authored metadata**.
It must not be reported as operational classifier precision/recall.
No ML inference is performed.

## Integration requirements — deferred deliberately

The project currently has versioned Memory admission/correction primitives
and Stage A semantic contracts but **does not yet** have a verified service
that converts arbitrary natural language and stored Memory into the
`ClaimScope` keys and time intervals required here.

Before enabling this for live Memory:

1. **Source-grounded adapter:** derive candidate scopes and modality from
   evidence-linked user turns; authenticate Memory snapshot as active,
   user/persona-scoped and current version. Never accept a generated
   `ADMITTED_ASSERTED` status or arbitrary claim keys as trust.
2. **Independent eligibility measurement:** on a newly human-adjudicated
   privacy-safe holdout, assess **actual** subject/time/context extraction,
   explicit correction recall, abstention, false conflict alerts and latency.
   The 21 fixtures are not a substitute for this.
3. **Owner-owned routing:** interactive correction messages may initiate
   a bounded Memory correction proposal; the owning domain enforces
   authorization, generation fencing, idempotency, evidence and optimistic
   locking. Tentative resolutions belong to Conversation/Cognition.
4. **Advisory NLI integration:** only after first three gates pass; no
   long-running NLI/LLM calls inside a DB transaction or mandatory sync
   foreground path. INT8 and cutoff remain provisional.

**Decision register:** approved policy encoded in a pure evaluation contract;
authoritative Memory behavior unchanged; data remains provisional;
live eligibility accuracy **not established**.
