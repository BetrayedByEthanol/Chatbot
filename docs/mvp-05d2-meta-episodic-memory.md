# MVP-05D.2 — Meta-episodic memory

Candidate implementation, 26 November 2026. Architecture v2.5.1 §§4.2, 5, 19.1–19.3, 20.1; PPA-US-033/034/035/036 (partial evidence), roadmap 05D, GAP-07/09/12.

## Ownership and processing

Memory owns persistent pattern snapshots and evidence links (migration 0047). Cognitive Engine proposes up to two patterns during its existing asynchronous range inference. The normal range prompt is unchanged if fewer than two eligible distinct historical reports match its bounded lexical cues. Candidate query returns at most 24 rows before filtering and six after exact-source/quote deduplication; no additional worker, scheduler, provider or inference call. Historical episode text is supplied as separate untrusted data, not instructions. Only exact model-visible episode IDs can be proposed. No previous hypothesis is itself evidence.

Require two supporting episode IDs with distinct source turns and normalized quotes, at most six total support/counter links. Both roles are pinned by scoped foreign keys and fingerprints of episode content and original user input; report times are preserved, not asserted as actual event dates. Apply 05B/C/D.1 eligibility, exact quotes and the existing bounded sensitive-source heuristic. Exclude explicitly entity-linked turns from this generic topic path. Patterns remain MODEL_INTERPRETED without a fabricated confidence score; source integrity does not prove semantic support or independent real-world events.

Owner admission runs inside the existing fenced cognitive transaction, with optional pattern writes isolated in a savepoint. Stale evidence or an expired dependency rolls back optional pattern rows and admission event/outbox without discarding unrelated cognitive reflections. Acquire the existing historical revision lock exclusively only after normal owner/FK/event writes; no locks across inference. Existing operation recovery replays pattern counts without inference.

## Revisions and invalidation

An exact normalized topic + hypothesis identifies a claim family. Different evidence produces an immutable new version, duplicate fingerprints do not append; a version cannot silently remove previous counterexamples. Different wording is not automatically merged or treated as contradictory. Current-version read eligibility rechecks every linked episode fingerprint, current source eligibility and source quote. Any invalid supporting or counter episode suppresses the entire current pattern; no fallback to older versions. Historical records are retained. No claim mutation grants action authority or changes facts/goals.

## Recall

An explicit standalone question such as "What patterns have you noticed about my exercise?" uses the existing Conversation model path and historical read fence with a separate bounded Memory view. Lexical topic matching only, at most three patterns and all selected support/counter references; skip a pattern that cannot fit rather than hide its counterexamples. Unknown/budget cases use a canonical abstention. The existing bounded regeneration covers concurrent source changes. Ordinary turns do not query the pattern store. The response audit event records supplied pattern versions and source-turn IDs, not generated claims. The authenticated search API returns current eligible patterns across sessions.

## Verification and open work

Regression cases cover schema bounds, distinct source reports, scoped discovery, one inference call, support/counter provenance, persisted version and replay deduplication, stale admission, withdrawn/corrected dependencies, latest-version invalidation, bounded recall and unknown fallback. PostgreSQL CI pending; no I/P/M or behavioral acceptance promotion.

A lexical cue is not a semantic classifier; paraphrased retellings of one experience can still be mistaken for distinct reports. General automatic contradiction detection, reconciling invalidated counterexamples, semantic claim merging, exact event dates, multi-source episode admission, relevance feedback and full derived-record deletion/retention remain open. No routine foreground pattern search, general graph, or automatic goals/actions. Exact matching and resource limits are initial implementation choices, not architecture amendments; real-model quality and latency must be measured.
