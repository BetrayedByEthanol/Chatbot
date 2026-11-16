# MVP-02C.1 — Natural conversational preference acquisition

Authority: Persistent Persona Agent Architecture v2.5.1 §19.3 and the
existing P2-04 Memory ownership and provenance contracts.
Baseline: merged MVP-02B.2 historical source-linked recall.

## Decision and scope

Extend the **existing asynchronous, SQL-only explicit-memory consumer** for a
small high-precision class of naturally expressed first-person preferences.
Do **not** introduce another parser service, inferred-memory store, extra
LLM call, migration, background worker or Goal mutation. This is the first
vertical slice of MVP-02C, **not** general semantic/episodic extraction.

Examples accepted after strict full-message validation and the positive
privacy allowlist:

| Input | Domain-owned memory candidate |
| --- | --- |
| `I enjoy gardening.` | PREFERENCE `user.enjoys_gardening = yes` |
| `I really like gardening.` | Same slot and value (corroboration) |
| `I enjoy reading.` | Independent slot `user.enjoys_reading = yes` |
| `I don't enjoy gardening anymore.` | Same slot, `no` (conflict if `yes` exists) |
| `I enjoy walking.` | `user.enjoys_walking = yes` |

The predicate is scoped to the **specific allowlisted activity**; multiple
positive interests must coexist instead of colliding in a generic
`user.hobby` slot. `yes/no` represent **self-reported enjoyment**, not proof
of actual practice, routine, future intent, health, or completed Goal. The
source turn and original exact quote are retained; no assistant-generated
interpretation is promoted.

The supported positive allowlist reuses the P2-04 non-sensitive hobby set,
adding only walking and jogging. `_supported_safe_claim()` checks the topic
*and* allowed value at the Memory persistence boundary, not only in the
parser. Temporal qualifiers (`lately`, `used to`), speculation (`maybe`),
quoted speech, multiple activities, unsupported categories, and sensitive
topics do not create candidate records.

## Existing domain-owned processing flow

1. A user statement is durably received in `conversation_turns`.
2. Existing P0 outbox/delivery invokes the selected persona's
   `memory.explicit_extraction.v1` consumer **outside the interactive
   response path**.
3. `parse_explicit_claim()` produces the existing typed `ExplicitClaim`
   for the bounded conversational grammar.
4. `_persist_claim()` verifies the safe predicate/value, immutable source
   turn, authenticated scope and selection, and reserves a uniquely
   source-linked candidate.
5. The Memory owner admits a new slot, corroborates identical evidence,
   reports older claims as stale, or creates a **CONFLICT** on disagreement.
   It atomically records revisions, source links, event/outbox and delivery.
6. A conflicting negative/positive statement does **not** overwrite
   authoritative memory. Only existing authenticated
   `approve_extraction_conflict()` can approve the correction using
   optimistic version checks.

No external model or database lock is held across an LLM call. Existing
PostgreSQL `memory_records` and `memory_revisions` remain authoritative;
search and conversation recall continue using `search_memories()` and
`contextual_memory_block()`.

## Acceptance and limitations

PostgreSQL acceptance covers exact source/provenance, positive and
negative conversational reports, multiple independent preferences,
corroboration, explicit review of contradictions, versioned approval,
new-session retrieval, user isolation, and high-precision abstention on
sensitive, quoted, hypothetical or time-qualified statements.

```sh
python -m pytest -q tests/test_phase2_extraction.py
```

The standalone real-model evaluation uses normal authenticated turns,
Memory worker ticks and a fresh conversation; review both the actual model
request and PostgreSQL snapshots:

```sh
bash scripts/run_persona_e2e.sh \
  --scenario scenarios/mvp02c1_natural_memory.json \
  --report artifacts/mvp02c1-review.zip
```

The model is asked to recall **reported enjoyment**, not invented exercise
progress. A human reviewer should verify that an ambiguous future activity
is not stored and the later contradiction is only a candidate requiring
authenticated user approval.

**Deferred MVP-02C follow-up:** broader natural paraphrases (e.g.
`I've gotten into gardening lately`), temporal experiences, model-generated
memory proposals, and richer semantic merging. These require typed
source-validated model candidates, per-source privacy/sensitivity and
temporal-scope controls, a no-automatic-admission policy for inference,
and measured model quality before changing storage policy. Reuse
Cognitive Engine's async inference where appropriate; do not run model
inference inside `process_delivery`'s database transaction.

This intentionally does not alter approved Goals, user intent or
conversational focus.
