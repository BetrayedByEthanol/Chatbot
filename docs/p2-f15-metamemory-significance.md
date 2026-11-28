# P2-F1.5 — MetaMemory and experience significance

Baseline `01a1577` (merged #148). [Phase 0](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46161535352):
2,467 passed, 3 skipped; [live acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46161535350) PASS.
Authority PRED-ROADMAP-01 §4/§6, v2.5.1 and reconciled backlog.
New candidate NOT RUN. No story/phase acceptance promotion.

## 1. Review of existing ownership and residual gaps

MVP-05D.5 already owns append-only recall observations, exact route/topic
hashes, resolution/reopening, delivered two-choice suppression, adjacent
authenticated feedback and passage-specific ordering. MVP-07B2.6 already
masks stale response evidence in open gaps and excludes invalid feedback from
effective review. Retain all of these. Feedback on an actual delivered
historical response remains possible, but is not fact correction.

Inspection of `metamemory.list_recall_observations` found that a withdrawn
question could still appear as an open gap because request and answer
eligibility were collapsed into one flag. `prior_clarification` and
`adjacent_clarification_key` checked response lineage but not the original
request/current raw reuse barrier. The original implementation did not
separate waiting for an already-delivered question from a new attention
candidate. Existing `memory_episodes` preserve model-proposed experiences
and exact source spans, but no explicit user-reported significance marker
survives the model omitting an episode, nor is there an attention-age
distinction. These are the targeted residuals; no duplicate gap,
pattern or episode store.

## 2. MetaMemory lifecycle

Migration 0069 centralizes current request/response eligibility and the
24-hour delivered exact-choice predicate. Select the latest non-budget
observation for each exact scoped route/topic before checking eligibility.
Invalidated latest question is UNAVAILABLE and suppressed from effective
gaps; never fall back to an older observation. An invalid answer with
an independently current question reopens the existing gap (AVAILABLE
becomes STALE in the effective read). Budget omission does not close or
replace the last substantive observation.

| Effective state | Meaning |
| --- | --- |
| OPEN | Latest eligible question still lacks usable evidence, or its prior answer is stale. |
| AWAITING_CLARIFICATION | Exact two-choice ambiguity and still-current, already-delivered question within existing 24-hour cooldown. |
| REVIEW_REQUIRED | Eligible explicit INCORRECT/MISSED feedback on the latest observation. |
| RESOLVED | Latest eligible owner read was AVAILABLE without current adverse feedback. This does not verify truth or delivery. |
| UNAVAILABLE | Request source itself invalidated. Not a current knowledge need. |
| SUPERSEDED | Older non-budget observation; audit only. |
| BUDGET_OMITTED | Budget omission; no knowledge conclusion. |

Original observation outcome, evidence references, feedback and delivery
timestamps remain audit history; `request_current`, `source_current` and
`gap_state` distinguish current eligibility and lifecycle. Effective
`open_only` reads expose current feedback only, and mask stale evidence.
`attention_only=True` with `open_only=True` selects bounded OPEN and
REVIEW_REQUIRED gaps (review first), excluding already-awaiting clarification,
superseded, resolved and unavailable. It is an owner review/attention read,
not a new scheduler, prompt injection, instruction or proactive callback.
Use candidate-first scoped selection before evaluating expensive predicates:
history checks only its requested page, open mode checks latest non-budget
per scope/key. The existing actual clarification path uses the same
source-current choice predicate. No broad semantic question generation,
gap discovery from absence or arbitrary model QUESTION proposal admitted.

## 3. Experience significance and attention

A deliberately narrow explicit pilot recognizes standalone authenticated text
"This experience is important to me: ..." (bounded to 600 chars, existing
sensitive-text guard; quoted/ambiguous forms abstain). This reports user's
significance judgment, not verified event, emotion, factual confidence,
retention instruction or inferred trait. It is ordinary user-provided
Memory metadata, independent of predictive enable/reset. No historical
backfill, voice admission, model-generated significance or new model pass.

Store only the source turn/event, scope and timestamps in immutable
`memory_experience_markers`. The original raw input is the authoritative
content, no copied value or summary required. An explicit marker can
survive the summary model omitting its episode. Active attention expires
after 168 hours from reception (provisional reviewable policy); aged
significance remains available while its source is eligible. No retention,
deletion, Memory confidence or stable preference changes.

The existing summary range includes a host significance tag only on eligible
marked turns and asks the existing model to consider their visible evidence.
The existing Cognitive range includes an active-attention tag for eligible
marked inputs. Both include supplied markers in the existing source digest
and late source/lease checks; ordinary unmarked digests remain unchanged.
A source correction, retraction, expiry or privacy withdrawal suppresses
the marker; age alone is not source invalidation. No independent
corroboration or automatic fact admission. Relevant historical episode
candidate selection uses active significance only as a tie-break after
existing lexical relevance (before recency); source eligibility, bounded
candidate count, deduplication and complete Cognitive episode pins are
unchanged. No foreground ranking change or unrelated prompt injection.
The authorized bounded `list_experience_significance` diagnostic returns
only eligible metadata, optionally active-only; no new transcript export.
Physical erasure of immutable metadata remains governed by DEC-03/07B3.

## 4. Verification and limits

New regressions cover explicit/ordinary/quoted/sensitive input, scope and
replay, real summary/cognitive lease and prompt projection, persistence
when summary omits an episode, source correction/retraction/expiry, aged
attention (synthetic elapsed-clock fixture), stale prepared owner commit,
relevant episode tie-break and exclusion, withdrawn latest question without
older fallback, actual delivery/cooldown and feedback review priority with
source expiry. Static review only; PostgreSQL execution and independent
review required. No extra inference, service, worker, background scan,
authoritative experience store or retention policy.

GAP-04/09/12 remain partial: generalized knowledge-gap discovery and
Cognitive QUESTION lifecycle, cross-route/adaptive retrieval feedback,
implicit significance assessment and calibrated salience, broader semantic
comparison, and held-out effectiveness/performance remain open. Gap
attention read is not yet a model-consumed cross-range reflection queue;
that would require additional exact provenance, invalidation and review
before activation. No predictive learning content is newly consumed;
P2-F1.6 integrated acceptance, P2-F2 attribution/mood/approach learning
and P2-F3 performance remain distinct. GAP-06 continuity and US-117/118
partial, US-085 open; no acceptance or deployment promotion.
