# MVP-07B2 — Persistent response lineage and revision invalidation

First reviewable vertical slice of US-117/118; architecture §§5, 7.1, 19.3, 20.1. Baseline `62f54a8945a9d57e559214c137c5ae1fc90ed84a` after PR #131, 2,278 PostgreSQL tests passed and live integration passed. Confirmed requirement: derived evidence cannot gain authority/verification, and corrected/withdrawn/expired evidence must stop influencing affected reads. The exact universal EvidenceLineage envelope is still proposed; do not introduce a second evidence store or a global provenance graph.

## Gap

Existing historical-recall revision and earliest expiry fence protects the interval from context preparation to response commit, then disappears. Canonical fact/entity rendering reads current owner evidence at commit, and Relationship preference recall checks exact versions. A previously generated or delivered response can subsequently expose withdrawn or corrected content on replay, delivery, or synthesis. A derived assistant reply can also become a fresh model input even when its underlying evidence is no longer current. Existing document excerpts are already omitted from derived conversation history.

## Persistent dependency contract

Migration 0057 adds `conversation_response_lineage` with a scoped FK to the immutable response ID and user/instance. The row contains:

- Historical scope revision and next known expiry (nullable).
- Exact Relationship preference keys and observed status/version tuples, including missing keys by absence (nullable set).
- Explicit legacy-unbound marker for pre-migration responses.

For existing historical-context routes, persist the already validated `HistoricalReadFence` in the same response transaction. Capture a scope fence for canonical personal-fact and entity recall before reading their owner view, validate it after owner read locks, and persist it too. Entity report writes are not combined with a historical SHARE lock. Persist the existing exact Relationship owner fence for tracked preference keys. Untracked normal conversation remains compatible; planning-only raw-text preference recall has no tracked owner key yet. Existing Memory recall observations and grounding events keep exact document/version/passage/digest, memory/entity/pattern IDs, source turn IDs, and epistemic distinctions where already present. They join through response ID. Do not copy source content, infer a new source version or treat a matching revision as independent verification.

The historical guard is deliberately conservative **scope invalidation**, not exact dependency matching. A tracked change elsewhere in the same persona (including summary/episode admission) or the earliest unrelated normal active expiry can invalidate an otherwise useful reply. Relationship checks are key-specific. Another user's or persona's revision changes do not invalidate this scope. No new historical revision writers in this increment. Owner correction/revocation and expiry remain authoritative; source content, trust and verification are not changed. Future exact per-source dependencies should reduce false invalidation once safe complete coverage exists.

## Output and replay

`require_response_lineage` reuses the Relationship state/row locks and historical revision SHARE lock in a short transaction. Existing selection/session/conversation checks remain authoritative. No locks span LLM/TTS calls or client I/O.

- Lookup, concurrent response replay, normal generated replay, application delivery offer and text ACK check recorded dependencies. Invalidated output raises `InvalidatedResponse`; application returns sanitized `STALE_EVIDENCE`. No new generation for a stale idempotent receipt; submit a new authenticated turn to get a current answer.
- Application status exposes CURRENT, INVALIDATED or UNTRACKED metadata, not content or a claim that untracked outputs are proven safe. Local acceptance recovery skips invalidated generated output.
- Speech checks before synthesis, clip admission and QUEUED/STARTED callbacks. A late PLAYED callback can remain truthful clip-history evidence but cannot mark a stale response delivered. No physical retraction, cancellation guarantee or ability to recall bytes/audio already emitted.
- A response remains GENERATED or DELIVERED as a historical fact. Invalidation is a separate read projection, not a rewrite, synthetic delivery event, or owner mutation. Revision/expiry checks occur at the last controlled boundary, not atomically with an external client receiving bytes. An already-offered response can become invalidated before ACK; the ACK is denied. Existing session authority fences still apply.

## Prevent new derived assistant copies

The shared SQL projection `response_lineage_derived_text` retains the established saved-document omission and omits other tracked/legacy assistant text from recent model context, summary and cognition ranges, episode source views and new automatic dialogue derivation. This is intentional even while the response fence is current: assistant text is a derived interpretation, not a new independent source. A fresh recall can read the original eligible source. This avoids copying known guarded assistant content into a new summary or reflection that lacks independent lineage. It also trades away some verbatim follow-up continuity. Existing owner-linked goal/procedure guidance and ordinary untracked response behavior remain unchanged by the projection.

The worker snapshot hashes the projected input, not a new private copy of the omitted assistant content. Snapshot readiness and automatic scheduling exclude invalidated GENERATED responses from pending-output barriers; actual delivery history is not changed. A tracked assistant reply prepared for a worker after this migration never supplies that reply's body, regardless of a concurrent correction. This is not a general late-worker revocation fence for raw user sources or older derived material.

## Migration and remaining gates

Migration 0057 records all prior responses as LEGACY_UNBOUND because their complete dependencies were never stored. No guessed historical revision or retroactive claim of safety. They remain in PostgreSQL for governed history; automatic raw replay/delivery and derived assistant reuse fail closed. Existing delivered status stays delivered. This affects even benign old responses; operate the migration with application/worker processes stopped, review recovery behavior and request fresh responses. No full upgrade/restore rehearsal has been executed. No automatic deletion or cleanup of original records.

This first 07B2 slice does **not** solve invalidated raw recent user turns, all ordinary model-context dependencies, existing summary/reflection/intent copies, general procedure/goal dependency propagation, cross-owner indexes/caches, or every background late-result path. In particular older summaries/episodes and cognitive proposals may still retain superseded content; existing owner eligibility and P0 fences continue to apply but are not a comprehensive new lineage gate. Continue 07B2 with owner-specific raw-turn and derived range lineage, eligibility/read masking and late worker commit guards before calling it complete. 07B3 policy-governed physical deletion, backups, external-provider/export copies and audit minimization are separate. No retention durations chosen, no legal compliance claim, and no remote/untrusted deployment approval.

## Regression

`tests/test_mvp07b2_response_lineage.py` tests durable generated/delivered replay/ACK after withdrawal, correction between response commit and offer, clock-only expiry, fact owner amendment, exact Relationship version and unrelated preference, summary/cognition projected input and readiness, late speech callback, tenant isolation, and legacy-unbound emulation. Existing knowledge/entity/meaning/recall, summary/cognition, authenticated application and speech suites remain mandatory. Legacy backfill test emulates the backfill row, not a complete in-place upgrade rehearsal.

```sh
python -m pytest -q tests/test_mvp07b2_response_lineage.py \
  tests/test_mvp07a_application.py tests/test_mvp05d4_document_knowledge.py \
  tests/test_mvp04c3_personal_fact_recall.py tests/test_phase2_summaries.py \
  tests/test_phase2_cognition.py tests/test_phase5_speech_output.py
```

Candidate static review only; PostgreSQL and live CI NOT RUN at authoring. US-117/118 remain partial; US-085 remains missing. Do not promote full 07B2 or deployment gate on this candidate.
