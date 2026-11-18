# MVP-02C.10A — Versioned memory-conflict dataset v2 and tentative-claim decision

**Status:** Architecture decisions **accepted by user**; dataset annotations
remain **provisional** pending independent, prediction-blinded adjudication.
This increment updates **offline evaluation contracts only**, not production
Conversation, Cognition, Memory, or NLI model routing.

## Accepted domain behavior

The confirmed decisions apply to the existing v2.5.1 owner boundaries:

1. **Tentative statements are not authoritative Memory facts.** Retain
   uncertainty with source and time context; do not promote a model's
   interpretation into `memory_records`.
2. **Later definite evidence may resolve a tentative conversational claim**
   without creating an authoritative Memory conflict. Conversation manages
   immediate uncertainty; evidence-linked longer-lived hypotheses belong to
   Cognition. The new definite claim is separately evaluated by the existing
   Memory admission owner and privacy/policy gates.
3. **A tentative statement cannot silently replace an admitted Memory record.**
   If it raises doubt about a current value, preserve the admitted source and
   unresolved uncertainty; clarify or use the existing domain-owned review
   path before any correction.
4. **Explicit first-person corrections must enter the existing authorized
   correction process independently of an NLI softmax cutoff.** Recognition
   is not self-authorizing: authenticated user, provenance, version/fencing,
   predicate/temporal scope and Memory ownership remain mandatory.
5. **NLI provides advisory textual evidence only.** Neither the model's
   argmax nor a `contradiction >= 0.90` mark determines admission, conflict
   eligibility, suppression, supersession, retraction or approval.

These are behavioral contracts to implement in future, not claims that all
existing runtime paths already execute them.

## Dataset v2

New: `scenarios/mvp02c10_memory_conflict_diagnostic_v2.json`

- **94** original identifiers retained: **23** draft contradiction,
  **10** entailment, **61** neutral, across **11** diagnostic slices.
- Exactly **five** reviewed wording proposals applied: MC035, MC039,
  MC041, MC048, MC089. Their draft NLI labels are unchanged.
- Exactly **three** cases assigned
  `expected_resolution=TENTATIVE_CLAIM_RESOLVED`: MC071, MC072, MC074.
  Their draft NLI remains **neutral**; none enters authoritative Memory
  conflict review.
- The original v1 source remains untouched at SHA256
  `2d28d7761920d743addd11e75d2af86b9f229756810bc0fffe6df6e947dc241b`.
  Dataset v2 records the parent hash and assistant second-pass review
  provenance. **Do not mix v1 benchmark scores with v2 scores**.

### Revised labels and their meanings

| Field | Meaning |
|---|---|
| `expected_nli` | Provisional *directional text* entailment, neutral or contradiction. |
| `scope_compatible` | Oracle: same subject/predicate and non-disjoint relevant time, **regardless of admission or assertion**. |
| `stored_claim_status` | Synthetic admission assumption: `ADMITTED_ASSERTED`, `TENTATIVE_UNADMITTED` or `OTHER_UNADMITTED`. **Not a real Postgres query.** |
| `memory_conflict_eligible` | Oracle comparability: `scope_compatible && stored_claim_status == ADMITTED_ASSERTED`. Eligibility is **not** a conflict finding or permission. |
| `expected_resolution` | `NONE`, `POTENTIAL_MEMORY_CONFLICT`, `EXPLICIT_USER_CORRECTION` or `TENTATIVE_CLAIM_RESOLVED`. These are expected behavior categories, **not executable commands**. |

Eligibility and scope are intentionally separate. In v2 **44/94** examples
have compatible underlying scope, but only **33/94** are eligible for
comparison with an assumed admitted assertion. A same-scope *entailment*
can be eligible yet require **no correction**, so
`expected_review = memory_conflict_eligible AND expected_nli == contradiction`
is retained for the diagnostic confusion matrix.

The **oracle admission flags are hypothetical benchmark annotations**.
Actual authoritative status, user identity, source timestamps, selected
persona, privacy class and domain owner must always come from existing
runtime evidence and policy checks.

### Resolved cases

| ID | Prior claim | New claim | Text NLI | Scope compatible | Prior state | Memory conflict eligible | Expected resolution |
|---|---|---|---|---|---|---|---|
| MC071 | I might enjoy gardening. | I do not enjoy gardening. | neutral | yes | tentative, unadmitted | no | tentative resolved |
| MC072 | I think my appointment is on Friday. | My appointment is on Thursday. | neutral | yes | tentative, unadmitted | no | tentative resolved |
| MC074 | I may still own an old bicycle. | I do not own a bicycle. | neutral | yes | tentative, unadmitted | no | tentative resolved |

*Resolution* means updating how the conversation understands a previously
tentative hypothesis. It does **not** mean persisting the later statement,
declaring the initial statement false, or approving a Memory mutation.

**Important example:** If `I enjoy gardening` had already been admitted
and the user said `Maybe I don't enjoy gardening anymore`, the prior
Memory record remains authoritative and the tentative evidence is not an
automatic Memory conflict or amendment. If the user instead gives an
explicit, definite correction, the existing Memory-owned correction path
applies regardless of the NLI score.

### Rewritten source pairs

- **MC035:** winter **2024** non-cycling vs cycling in **autumn 2026**.
- **MC039:** planned **running club membership** vs currently running alone.
- **MC041:** planned **advanced Italian course** vs current fluency.
- **MC048:** hypothetical **guitar lessons** vs current playing.
- **MC089:** German explicitly says **owns** a bicycle (`besitze`), not just
  `habe`, to match the English ownership denial.

No other statement text was changed.

## Running v2 on the local model machine

The existing offline runner accepts **both** strict versions; its default
is now v2. The report records `suite_version`, `suite_sha256`, annotation
status and per-resolution metrics; all other FP32/INT8 comparisons remain
unchanged and reproducible.

```bash
PYTHONPATH=src python scripts/evaluate_memory_conflicts.py \
  --suite scenarios/mvp02c10_memory_conflict_diagnostic_v2.json \
  --model-dir "$HOME/models/nli/finecat-nli-v2-xl" \
  --precisions fp32 int8 --threads 2 --warmup 1 --repetitions 2 \
  --report artifacts/mvp02c10a-memory-conflict-v2-fp32-int8.json
```

To reproduce the immutable earlier benchmark, explicitly pass
`--suite scenarios/mvp02c10_memory_conflict_diagnostic.json`, and use
a different new report file. Both commands are read-only and require locally
cached models; neither changes persona state.

## Acceptance and remaining evidence gaps

- Offline tests verify **v1 remains unchanged**, same 94 IDs,
  **exactly five** rewrites, all three tentative-resolution decisions,
  explicitly versioned report provenance, mutually consistent v2 fields,
  and raw versus oracle-only alert scoring.
- Review **all v2 annotations independently without model scores**;
  a human reviewer should record ambiguity and disagreements. User-approved
  *architecture contracts* are not equivalent to independently approved
  *semantic gold labels*.
- **Do not** claim production precision/recall, adopt `0.90` as calibrated,
  promote FineCat INT8, or implement automatic Memory correction from this
  synthetic dataset. Evaluate on a separate representative holdout and
  measure actual context-eligibility classification first.

**Decision register:** v2 schema and tentative-case behavior accepted;
v1 benchmark frozen; v2 diagnostic evaluation pending on local model;
independent label review and production integration not approved.
