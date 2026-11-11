# P2-01 — Memory Persistence & Retrieval Foundations

Authority: Persistent Persona Agent Architecture v2.5, Sections 5, 9, 14, 19,
20 and acceptance cases C02–C05, C14. This is the first **Phase 2** vertical
slice, not a claim that all Phase 2 memory/cognition is implemented.

## What is implemented

- Typed `AdmitMemoryCommand` and `AmendMemoryCommand` for **explicit,
  source-evidenced** FACT, PREFERENCE and PERSONAL_MEANING records.
- PostgreSQL `memory_records` (current version) plus append-only
  `memory_revisions` (past values, source turns, event IDs, confidence).
- Strict `(user_id,instance_id)` scoping, authenticated user-source turn quote
  checks, selected Foreground persona generation fencing, per-record
  version checks, scoped command idempotency, atomic event/outbox writes.
- Contradictory values for the same type/subject/predicate are **rejected**.
  A newer explicitly evidenced correction must name the memory ID and
  expected version. A retraction hides the record from retrieval without
  rewriting revision history. Retraction is not a full privacy erasure.
- Bounded full-text search with optional source-date filters, scoped reads,
  normal-privacy-only admission and retrieval, deterministic ordering and
  source turn anchors. No vector service or cache is required.
- Explicitly cued historical recall adds **optional** user-reported memory to
  the existing prompt only when budget allows. Most turns run no memory
  query. Recent verbatim text and active dialogue continue to take precedence.

## Domain ownership and trust

Memory is a separate owner from Conversation, Cognitive, Goal/Task, Emotion
and Procedure. Input text remains an immutable Conversation-domain source.
A matching quote establishes that the source user made a statement, **not**
that the parsed claim is true. `USER_REPORTED` is not an independently
verified state. Confidence describes extraction quality, not fact certainty.

The Context Builder labels retrieved memories as historical
**user-reported**, never as developer/system instructions or permission
grants. This API does **not** allow automatic model output to commit a
verified user fact; future extraction may propose `AdmitMemoryCommand`,
but a trusted domain layer must validate the interpretation.

Unique memory slots suppress exact repeated claims; separate user turns
repeating an identical fact currently do not add corroboration links.
The source of the first accepted claim remains durable. This is a known
limitation to address when semantic consolidation arrives.

## Failure and concurrency contracts

1. New writes require a selected Foreground generation and source turn
   owned by the same User × Persona. Other-persona and cross-user sources
   fail before any durable state changes.
2. Writes own a short transaction and never execute inference or network
   calls under locks. Duplicate commands replay receipts. Different
   commands for the same slot serialize through PostgreSQL uniqueness and
   row locking; different slots do not share a memory-row lock.
3. A stale expected version or older evidence cannot amend newer memory.
   Events and outbox rows commit with accepted revisions; failed proposals
   leave no committed receipt or event.
4. Historical search is read-only, bounded and can inspect the requesting
   user's inactive persona without selecting or activating it.
5. PostgreSQL is authoritative. The full-text index is derived and
   contains only current active entries for ranking; superseded memory
   revisions are excluded from search.

## Legacy migration

Retained from the legacy extractor: the candidate distinction among facts,
preferences, meanings and source quote/confidence. Rejected: hard-coded
example injection in `extract_facts.py`, global mutable STM, unverified
automatic selection of a highest-salience conflicting value, and the
LangGraph/Redis scheduler as a source of truth. No legacy classification
or salience constants are silently adopted.

## Validation and remaining Phase 2 work

`tests/test_phase2_memory.py` runs real PostgreSQL tests for restart
continuity, retrieval in a new conversation, no irrelevant prompt
injection, duplicates, explicit correction, conflicting claims,
retraction, idempotency collisions, scope isolation, persona switch,
timestamp bounds, parallel corrections and context budgets.

Still deferred to subsequent Phase 2 slices: automatic candidate extraction,
incremental range summaries, episodic/meta-episodic consolidation, retrieval
feedback and topic-aware hybrid ranking, asynchronous reflection with
watermarks, selective emotional inference and durable relationship state,
sensitive-memory consent and retention/erasure propagation. Keyword
recall detection is intentionally conservative; ambiguous references
require conversation-level clarification rather than invented memories.

Phase 3 Goals/Strategies/Procedures remain outside P2-01.
