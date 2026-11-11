# P2-04 — Explicit Memory Extraction & Consolidation

Architecture authority: Persistent Persona Agent Architecture v2.5, particularly §§4.1, 5, 9, 19.3, 23.1–23.2. This is a bounded automatic first slice, not general free-form model-based semantic extraction.

## Ownership and trust

P0 owns durable event delivery, retry leases and acknowledgments. Conversation owns authenticated, immutable user input. The Memory domain owns proposed claim records, normalized factual/preference/personal-meaning records, versioned memory revisions, support links and correction decisions.

A standalone operator-owned `memory_extraction` worker subscribes to `conversation.user_text.received.v1`. Each authenticated event is processed in P0's **database-only transactional delivery handler**: scoped source lookup, selected-persona fence, bounded extraction, candidate reservation, memory-domain semantic/consolidation decision, event/outbox (when applicable), and delivery ACK all commit or roll back together. There are **no LLM calls or external tools** in the response path or event transaction.

The currently supported high-precision grammar recognizes only short, single-sentence first-person assertions:

- `My favorite drink is tea.` → PREFERENCE `user.favorite_drink = tea`
- `I prefer tea over coffee.` → PREFERENCE `user.prefer_over_coffee = tea`
- `My hobby is gardening.` → FACT `user.hobby = gardening`
- `My dog's name is Milo.` → FACT `user.pet_dog_name = Milo`
- `For me, home means feeling safe.` → PERSONAL_MEANING `user.meaning_home = feeling safe`

The supported **topic-and-value vocabulary is a positive allowlist**, not a syntactic pattern plus a blacklist. Automatic NORMAL-privacy admission currently recognizes small predefined sets of non-sensitive drink, food and color favorites; specific hobbies and activity preferences; a bounded set of pet names; and a few non-sensitive personal meanings. Unsupported topics **or** values (including medication names, religious institutions, and medication comparisons) are discarded from automatic extraction, not persisted as NORMAL memories or candidates. The Memory persistence boundary repeats this safety check even for candidates supplied without the normal parser. Any expansion requires privacy review and regression tests.

This is deterministic **claim extraction** from an authenticated utterance, *not verification that the statement is true*. New records retain `USER_REPORTED` epistemic status and the original user source ID. The memory admission event is marked `TRUSTED_PLATFORM` rather than falsely reporting that the user invoked the P2-01 admission API. Confidence is parsing quality, not factual certainty.

Model inference, complex corrections, quotations, multi-statement text, speculative wording, instructions embedded in user content, and sensitive-looking fields are deliberately **not auto-admitted**. The narrow grammar is expected to expand later behind a validated proposal-and-review gate, without weakening Memory-domain authority.

## Consolidation contracts

Migration `0011_memory_extraction.sql` adds:

- `memory_extraction_candidates`: immutable source-event and turn links, normalized slot, candidate value, decision status and target memory/version. A `PENDING` reservation is written before consolidation, then atomically finalized; the unique source-turn/slot key prevents duplicate processing on event redelivery.
- `memory_claim_support`: additive, source-anchored corroboration **scoped to a specific immutable memory revision**. Old support is never silently carried onto a revised value.

Outcomes:

1. No existing slot → admit the narrow, evidenced claim to P2-01 `memory_records`, `memory_revisions`, `events` and `outbox`.
2. Same active slot and value with sufficiently recent evidence → `CORROBORATED` candidate and evidence link; no fake new fact version.
3. Different value, retracted slot or contradictory later claim → `CONFLICT`; existing record is unchanged and a memory conflict event is emitted for downstream review.
4. Older historical evidence than the currently authoritative slot → `STALE`; never downgrade newer memory.

`list_extraction_conflicts()` exposes a scoped review queue. Before listing, it performs a bounded, transactional re-evaluation of any `CONFLICT` candidates still pinned to an obsolete memory revision. A candidate whose immutable source turn predates the newly authoritative source is retired as `STALE`; a newer candidate matching the current value becomes revision-scoped `CORROBORATED` evidence; and a newer conflicting candidate is retargeted to the current version for **fresh authenticated approval**. No re-evaluation changes the authoritative memory value. The approval endpoint also re-checks its locked candidate against the current revision, so stale review versions cannot bypass optimistic concurrency.  Only an authenticated user can call `approve_extraction_conflict()`, which uses the same Memory-owned correction primitive as the already-merged P2-01 `amend_memory()` API. Its original, exact command can replay after selection changes; fresh corrections require the selected generation. Both paths enforce expected version, newer source evidence, idempotency and revision/outbox invariants. A model or platform consumer cannot silently approve contradictory values. Other user corrections still use P2-01 directly.

**Atomic review invariant:** `approve_extraction_conflict()` opens one PostgreSQL transaction, locks the candidate row (`FOR UPDATE`), performs the Memory-owned correction within that transaction, and marks the candidate `APPROVED` with the committed revision in the same transaction. The memory record, revision, command receipt, event, outbox and candidate update commit or roll back together. Concurrent approval requests serialize on the candidate lock and replay the original receipt without producing a second correction. A crash after the memory primitive has performed its writes but before the candidate decision **must** leave no durable correction, no receipt or outbox event, and the candidate still in `CONFLICT` for safe retry.

## Lifecycle, delivery and recovery

P0 per-stream delivery order prevents ordinary events from overtaking their predecessors. Inactive-persona claims are **deferred**, not discarded, and are resumed after reselecting the same persona. P0 expired delivery claims are fenced and reprocessed. The database transaction makes insertion/revision, candidate decision, and event ACK all-or-nothing. The memory unique slot and candidate uniqueness protect concurrent user and worker admissions.

Register the subscription **before** enabling outbox dispatch. Late deployment can use existing P0 `backfill_subscription()` (explicit authenticated per-persona replay) to recover historical user inputs not subscribed at initial dispatch. This slice does not introduce a second scheduler or automatic wide historical scan.

## Operator setup

Apply **all ordered PostgreSQL migrations through 0011**, including P2-03 migration `0010` and new `0011_memory_extraction.sql`, before launching workers.

    DATABASE_URL=postgresql://... python -m persona_agent.memory_extraction --poll-seconds 1

One bounded cycle:

    DATABASE_URL=postgresql://... python -m persona_agent.memory_extraction --once

This worker is independent of the P2-03 summary scheduler. It consumes only authenticated conversation input events and performs no model inference.

## Acceptance and deferrals

`tests/test_phase2_extraction.py` uses real PostgreSQL to validate: explicit fact/preference/personal-meaning parsing, persistent provenance and cross-session recall, revision-scoped corroboration, conflicting candidates without silent overwrites, explicit user-approved versioned correction, sensitive/ambiguous statement exclusion, inactive persona deferral/resumption, and stale-evidence protection.

Deferred after this slice: generalized LLM-driven memory proposals, richer semantic/entity normalization, semantic graph merging, fuzzy corroboration, typed sensitive-memory consent and retention/erasure propagation, relevance feedback and full P2-05 hybrid attention/ranking. These require explicit validation/evaluation before enabling broader automation.


## P2-04 review hardening — atomic conflict approval

A Codex review identified a two-transaction failure window: the P2-01 correction committed independently before the extraction candidate's approval marker. The fix factors a narrowly scoped `_amend_memory_in_transaction()` primitive inside Memory while retaining the public `amend_memory()` transaction contract. The extraction approval caller now holds the candidate lock and invokes that primitive through the **same PostgreSQL cursor and transaction**, then finalizes the candidate before commit. PostgreSQL regressions inject a failure after all memory effects are written but before candidate finalization, assert complete rollback (including revision, receipt and outbox), retry successfully, and verify concurrent duplicate approvals produce exactly one revision.


## Codex review hardening — sensitive-topic gate and conflict re-evaluation

- **P1 privacy:** First-person syntax does not imply consent for NORMAL-privacy storage. Both `parse_explicit_claim()` and the internal Memory candidate persistence boundary require a supported, positive allowlist match for the topic **and the value**. Sensitive and unsupported statements (e.g. favorite medications, churches or religious meanings) create **no** authoritative memories, source-linked candidates, or contextual-recall entries. The existing blacklist is additional defense, not the admission policy.
- **P2 conflict lifecycle:** The scoped review API reconciles obsolete `CONFLICT` decisions in bounded, short PostgreSQL transactions. Evidence chronology is taken from immutable source-turn timestamps relative to the latest authoritative record source timestamp. Old evidence is retired; matching newer evidence becomes corroboration on the current revision; newer contradictory evidence is offered for explicit user approval with its **current** expected version. The correction endpoint also re-evaluates a locked candidate before P2-01 version checks. This preserves user ownership, concurrency protection and audit history without silently rewriting personal memories.

PostgreSQL regressions cover unsafe topic *and* value rejection at persistence, approval of two differently valued conflicting turns in chronological order, retiring older conflicts when newer evidence wins, and turning matching newer conflicts into revision-scoped corroboration.
