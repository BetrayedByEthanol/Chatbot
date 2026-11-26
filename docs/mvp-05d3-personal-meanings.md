# MVP-05D.3 — Semantic memory and personalized meanings

Candidate implementation, 26 November 2026. Architecture v2.5.1 §§5, 19.1–19.3; PPA-US-030/032/034 (partial evidence), roadmap 05D. No architecture or acceptance promotion.

## Ownership

Reuse the existing PERSONAL_MEANING slot in memory_records and its immutable memory_revisions, explicit ingress reports, automatic extraction worker, conflict review, owner correction, receipts and events/outbox. Migration 0048 adds version-pinned constituent spans and separate rule-inferred concept links. They are projections of an active revision, not a second definition owner. All insertions happen in the same transaction as the source revision; no new scheduler, model request or procedure mutation. No data migration or automatic reinterpretation of existing sources.

A supported explicit report is "When I say evening routine, I mean preparing tomorrow's lunch, walking the dog and reading for 20 minutes." The definition keeps the entire authored value; components preserve the original ordered spans. Rule associations classify only allowlisted actions (e.g. walking the dog -> pet_care), carry RULE_INFERRED and a rule version, and never alter the user definition. No model-inferred personal beliefs, action completion, habit, goal or permission claims.

## Admission and correction

A dependency-light complete-statement grammar accepts existing home/friendship/learning terms and bounded morning/evening/weekend/bedtime routine definitions. A positive allowlist of values/actions, max 100-character definition and at most five distinct components preserve the existing NORMAL privacy admission boundary. Known reading duration is bounded to 1–120 minutes. Exact whole-source and value matching is required for projection, including direct typed Memory admissions; unsupported typed records keep their preexisting behavior without structured projection. Unsupported or ambiguous free-text definitions are not auto-admitted; broad arbitrary phrase and correction interpretation, including suppression of older meanings after unsupported corrections, remains open.

Recognized newer conflicting reports suppress the old current definition immediately via the existing read eligibility predicate, before worker consolidation. A reviewed correction appends versioned components and associations. Retraction, expiry and sensitivity changes hide the full current view without deleting prior source history. Historical read revisions fence both direct and model recall; no lock spans model inference.

## Retrieval

Exact personal questions ("What do I mean by evening routine?") route to a canonical owner-grounded reply without inference. The forced model path uses the same commit guard. Unavailable/disputed/unsupported source returns an explicit unknown; no raw transcript fallback or model guessing. A trusted audit event records the definition revision and source used. Bounded explicit routine-use requests ("Help me plan my evening routine") receive the versioned definition, components and separately labeled optional inferred links in existing Conversation context. No query for ordinary unrelated turns; no new context builder or tool authority. Inferred associations are not synonyms or instructions and cannot independently resolve a definition.

The scoped read API exposes exact current source, confidence, version, complete definition, constituent spans and optional associations. Legacy exact definitions may be read as derived source spans without retroactive stored associations. Generic recall behavior and previously stored non-grammar personal meanings remain unchanged. General derived lineage and privacy/retention policy are still MVP-07B work.

## Verification and limitations

Regression coverage includes accepted/rejected grammar, exact span provenance, scope, direct/forced-model recall, context budget/routing, ingress conflict suppression, reviewed revisions, replay, retraction, privacy/expiry and invalid source projection. PostgreSQL CI pending. No claims of measured performance or full behavioral acceptance. Rule-based associations are an intentionally narrow deterministic slice; general cognitive/semantic associations and free-form definitions need separate evidence, privacy review and evaluation.
