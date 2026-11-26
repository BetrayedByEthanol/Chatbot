# MVP-05D.5 — MetaMemory and retrieval feedback

Candidate implementation, 26 November 2026. Architecture v2.5.1 §§5, 19.2–19.3, 22.1; MEM-05/MEM-08, PPA-US-030/031/034/092; GAP-04/GAP-09. No acceptance promotion.

## Ownership and lifecycle

Memory owns an append-only recall observation projection (migration 0050) for the existing explicit FACT, MEANING, named ENTITY/ENTITY_HISTORY, PATTERN and KNOWLEDGE recall routes. Conversation records the owner-read result in the successful response transaction, not the model's claim or confidence. Existing grounded-response event/outbox remains the provenance journal. Store route, normalized exact-topic hash, response/turn IDs, bounded evidence references and status; no new copies of memory values, names, source passages or query text. The existing sensitive-text heuristic excludes matching topical queries; hashing is not anonymization.

AVAILABLE means source evidence was available, not that the answer or an external fact was verified. UNKNOWN, AMBIGUOUS, CONTRADICTORY (owner dispute), UNVERIFIABLE (unsupported source), STALE (explicit expired normal personal-fact slot), NO_EVIDENCE and BUDGET distinguish observed limitations. No absent-memory inference. An authenticated scoped read exposes observation history or latest non-budget unresolved gaps; a later successful recall records evidence-linked resolution, and another miss reopens the gap. Budget omission does not close a prior gap. Feedback indicating incorrect/missed information flags the latest observation for review; subsequent reads are new observations, not adjudication of earlier complaints. No continuous scan, automatic knowledge-gap inference, new worker, or model pass.

For exact named two-choice ambiguity, suppress re-asking unchanged previously delivered choices within 24 hours, preserve the visible choices and existing adjacent clarification contract. New candidate versions, no prior delivery, or later successful resolution prevent inappropriate suppression. Do not infer a permanent identity binding from a conversational selection.

## Retrieval feedback

The authenticated host can submit RetrievalFeedbackCommand tied to an adjacent user turn after actual delivery: "That recall was relevant.", "That recall was irrelevant.", "That recall was incorrect.", "You missed relevant saved information.", or "Saved passage 1 was irrelevant." (also relevant). Exact outcome/ordinal and response scope must match; one feedback per source turn; retries replay via receipts. Evidence is the user's report, not verified factual correction. Event/outbox record feedback ID, not content. No automatic interpretation of arbitrary user feedback.

For document retrieval only, a passage-specific RELEVANT/IRRELEVANT report changes ordering for the same normalized query, same user/persona and exact document version/position/content digest. Most recent relevance report wins; irrelevant results rank after other eligible matches but remain eligible. Positive feedback removes the demotion, not a truth/confidence boost. No effect on other queries, superseded sources or restricted/expired documents. Metadata writes do not advance historical evidence revisions or acquire their exclusive lock. Document ordering is a single-statement snapshot at recall commit; no additional inference.

## Verification and follow-ups

Regression cases cover owner outcomes, scope, gap resolution/reopening, conflict and source eligibility, delivered clarification suppression, exact evidence-bound feedback, replay and rollback, query/version-specific reranking and privacy. CI pending, static review only. Current gap lifecycle is recall-driven, not continuous semantic assessment. General Cognitive QUESTION-to-gap proposals, natural-language feedback ingestion, generic retrieval telemetry (unnecessary retrieval, false recall and omissions outside explicit routes), cross-route ranking, long-term clarification policy, retention/deletion and held-out real-model evaluation remain open. No changes to factual confidence, owner eligibility, procedure execution or goal authority.
