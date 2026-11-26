# MVP-05D.4 — Document and procedural knowledge

Candidate implementation, 26 November 2026. Architecture v2.5.1 §§5, 19.1–19.3; MEM-04, PPA-US-033, roadmap 05D. No acceptance or architecture promotion.

## Scope and ownership

The current runtime has a curated, approved Procedure Manager library with immutable shared template versions and private pinned variants, but no durable document knowledge store. Migration 0049 adds Memory-owned private supplied text, version metadata, exact indexed passages and optional links to existing private procedure revisions. No shared-publication or execution authority changes, no network crawler, model inference, document parser, new worker or scheduler.

The authenticated host submits validated SaveKnowledgeCommand / WithdrawKnowledgeCommand. A separate authenticated approval turn binds the entire payload (text, metadata, privacy, expiry, scope, version and procedure links) by digest, with the existing generation, evidence, idempotency, transaction and event/outbox contracts. Text is supplied out of band rather than copied into conversation turns or event payloads. The source locator/revision are supplied metadata, not independently verified authorship or a live URL. The initial scope is private User × Persona only; no cross-user sharing or operator archive export. NORMAL permits recall, RESTRICTED excludes it; expiry and withdrawal suppress current reads.

Each revision preserves original text (up to 24,000 Unicode characters), SHA-256, source type/locator/revision and approval provenance. Deterministic bounded passages preserve exact Unicode codepoint offsets and verify the content hash and original substring at retrieval. No LLM-generated extraction or factual promotion. Current-version FTS retrieval has bounded hit and character budgets; superseded versions do not reappear and withdrawn documents cannot be reopened by a stale command. History remains for later policy-governed retention. A new document revision must explicitly carry any procedure references; links pin private variant revisions, never silently follow a variant revision or document replacement.

## Conversation and access

Explicit standalone requests such as "What does the saved documentation say about certificate?" use the existing response transaction and historical revision/expiry fence. Canonical quoted excerpts with source locator/version/offset are read from current eligible rows at commit, including forced-model recall; unknown is explicit. No raw historical fallback or ordinary-turn knowledge search. The grounded audit event records passage identifiers/offsets, not document text. Document replies are marked as excerpt data; recent response context, summary/cognition source projections, episode-source reply restoration and dialogue delivery interpretation substitute a neutral placeholder rather than expose source text to a model or infer executable dialogue progress from it. Existing authenticated transcript replay and already-delivered text are retained; full end-to-end erasure and revocation after response commitment are not claimed (MVP-07B).

Procedure links are read-only Memory references; search_knowledge can filter by private variant ID and requires that its pinned revision is still current. No procedure runs, goals, strategy changes, tools or publication are admitted from knowledge content. General semantic synthesis, source freshness/credibility validation, per-document access grants, external ingestion (PDF/HTML/repository adapters), passage-level step references and shared knowledge remain future increments. Exact keyword recall is deliberately narrow.

## Verification

Tests cover Unicode offsets, narrow routing, exact approval and replay, private scope, bounded retrieval, source hashes, canonical no-model and forced-model recall, version/withdrawal fencing, restricted/expired/corrupt sources, pinned procedure revisions and exclusion of excerpts from model source projections. PostgreSQL integration CI pending; static inspection only, no performance or behavioral acceptance claim.
