# MVP-02C.12 — Source-grounded Memory eligibility integration (shadow)

**Status:** Implemented a narrowly scoped read-only PostgreSQL-backed
vertical slice. **No production decision routing or Memory writes changed.**
This is a real source/record integration test, not an operational general
language eligibility classifier or representative accuracy study.

## Existing contracts reused

- Conversation: `conversation_turns`, selected Foreground persona
  `persona_selection` generation and `TrustedActor`.
- Stage A: `RuleSemanticClassifier`, `SemanticAnnotationBatch` and
  `validate_semantic_evidence`.
- Memory: `memory_records` scoped by actor and persona, active state,
  NORMAL privacy, expiry, and unresolved correction suppression; original
  source quote and evidence turn, current Memory ID and optimistic version.
- Explicit extraction: `parse_explicit_claim` and its established
  **positive-allowlisted** activity-preference keys.
- MVP-02C.11: pure `evaluate_conflict_eligibility` decision contract.

The implementation is `src/persona_agent/memory_conflict_grounding.py`,
exposing:

```python
observe_grounded_memory_eligibility(
    connection,
    actor,             # authenticated TrustedActor
    instance_id,       # selected persona
    generation,        # expected selected generation
    source_turn_id,    # persisted, authenticated user text
    classifier=None,   # defaults to existing deterministic Stage A baseline
) -> GroundedEligibilityResult
```

The caller **does not pass user text**, identity, claimed memory admission,
target Memory ID, synthetic scope labels, nor classifier-created
subject/predicate metadata. Those must be derived from authenticated
PostgreSQL source turns, the reviewed deterministic grammar and Memory-owned
record metadata.

### Data flow

```text
TrustedActor + source turn ID + expected selected generation
    -> READ ONLY repeatable-read snapshot: selected Foreground; actual user turn
    -> close transaction
    -> Stage A classifier + verbatim source evidence validation
    -> narrow deterministic source-to-slot extraction
       (currently only user's current gardening/reading/walking enjoyment)
    -> READ ONLY repeatable-read snapshot: recheck selected generation,
       retrieve EXACT active/normal/unexpired memory slot and real source turn
       and exclude records suppressed by unresolved extracted conflicts
    -> close transaction
    -> revalidate stored memory source quote and canonical value/slot
    -> MVP-02C.11 pure eligibility gate
    -> read-only disposition / abstention / no-record outcome
```

The two short DB reads avoid keeping database transactions open while a
classifier runs. No locks, ML inference, Memory mutations, events, commands,
background subscribers or schema migrations are added. A persona switch
during model inference invalidates the second snapshot. A later persona
switch or revision **after** the result still makes it stale: domain-owned
actions must always recheck identity, selection generation, source evidence,
Memory version and authorization.

### Deliberately bounded grammar

Only **CURRENT**, **ASSERTED** first-person enjoyment statements supported
by both Stage A's existing exact rules and Memory's existing safe extraction
grammar can be mapped to a real canonical slot. Examples:

| New authenticated turn | Existing slot | Observation |
|---|---|---|
| `I really enjoy gardening.` | `PREFERENCE/user/enjoys_gardening=yes` | `NLI_CANDIDATE` |
| `I don't enjoy gardening anymore.` | same | `CORRECTION_PATH` |
| `I love reading.` | gardening only | `NO_ACTIVE_MEMORY` |
| `I used to enjoy gardening.` | gardening | `GRAMMAR_ABSTENTION` |
| Unsupported natural language | any | `SEMANTIC_ABSTENTION` |

The baseline classifier's correction/pragmatic label cannot be overridden
by an injected model. Proposed labels must match the existing deterministic
reference exactly, or the observation abstains. Invented evidence quotes
are rejected rather than used. The integrated evaluator never performs
textual NLI: `NLI_CANDIDATE` means **scope eligible**, not a contradiction.

**Critical limit:** When source text is not supported by existing rules,
it abstains. There is no speculative canonical subject/predicate/context,
no fallback `general` for unknown semantics, and no assumed temporal
validity for opaque Memory records. This is a high-precision narrow bridge,
not a broad language capability. It cannot yet resolve MC071/072/074, which
require new Conversation-owned tentative source tracking, or MC065 pet-name
context, which requires validated per-entity scope derivation.

### Security and privacy

- The user/persona scope, selected generation, and real input text are
  loaded from PostgreSQL. Foreign turns or users raise permission errors.
- The current exact Memory slot must be **ACTIVE**, **NORMAL**, unexpired,
  with a matching source quote and value, and no newer unresolved conflict
  suppression (same predicate as Memory recall). Otherwise it is not used.
- Only an admitted Memory ID/version from the DB may reach MVP-02C.11.
  A result is not a capability token, endorsement, correction request, or
  authorization to change a record.
- The diagnostic result omits source text and remembered values. No
  query data is logged or sent to another service.

### Tests and interpretation

`tests/test_mvp02c12_grounded_eligibility.py` uses **real PostgreSQL**
with normal schema and conversation/Memory owner APIs to verify:

- Source-backed admitted Memory → NLI eligibility (including same value).
- Source-backed explicit correction → correction *proposal* without NLI.
- Missing, expired, unsupported and mismatched-source Memory slots.
- Cross-user access denial and selected-generation fencing.
- Rejected injected semantic correction labels and hallucinated quotes.
- No Memory record, event or revision changes from observations.

Tests measure integration behavior on the narrow deterministic grammar,
not arbitrary text extraction accuracy. Existing 94-case synthetic v2
dataset and 21 hand-structured fixtures still **must not** be cited as
real-world eligibility performance.

## Remaining gates

1. Extend grounded statement, temporal, entity and context extraction
   with a separate **independently annotated, untouched** evaluation set
   before accepting general-language scope decisions.
2. Introduce Conversation-owned tentative-claim references before trying to
   route `TENTATIVE_RESOLUTION` from real persistent state.
3. Add a Memory-owned read-only adapter for more real active slots with
   source-aware temporal/context metadata (the current memory table
   stores no generic temporal validity scope).
4. Only after the above and security review: authorize shadow collection
   across a bounded range of real turns, assess false alarms and abstentions,
   then consider advisory NLI routing. NLI's 0.90 remains diagnostic,
   INT8 unapproved for production memory updates.

**Decision register:** Source-grounded **limited** shadow contract implemented;
Memory and Conversation remain owners; no production routing changes;
broader real eligibility quality remains unknown.
