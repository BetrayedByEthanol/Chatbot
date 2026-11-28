# P2-F1.4 — Subjective projection and conversational intentions

Baseline `7417348` (merged #147). [Contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46154528792):
2,449 passed, 3 skipped; [live acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46154528818) PASS.
Authority PRED-ROADMAP-01 §2–6.2, Architecture v2.5.1 and reconciled backlog.
New candidate NOT RUN. The original owner and phase gates retain authority.

## Scope and observable behavior

Compose an eligible, bounded Cognitive expectation comparison with the
existing Context Builder, stable persona, Relationship preferences and
Emotion presentation. Only an exact current supported dated activity
report can use this pilot; the comparison must pin a preexisting current
TENTATIVE directional expectation, a distinct occasion and complete
eligible raw sources. Do not copy learned prose, global traits, confidence
scores, history counts, forecasts or free-form Cognitive intentions into
the model prompt. No established maturity yet, so no strong
expectation-based surprise. Emotion's pure `tentative_reaction_hint`
provides low-key warmth for a positive mismatch and nonjudgmental support
for a reported setback. This is expressive context, not persistent mood,
a claimed user emotion, verified task completion or a policy update.

Ordinary EXPECTED reports without a separately applicable intention,
UNKNOWN, contradictory, duplicates, unsupported inputs, unrelated turns,
cold start and disabled learning do not add subjective presentation.
Current explicit request, preference and mandatory owner routes win.
Conservative pilot: any active stored proactive prohibition or topic
boundary suppresses the optional contribution (including source-under-
review boundaries; no guessing a topic match). Existing preference/affect
context remains independent. No extra inference. The block is appended
only if it fits after existing retained context, at most 420 chars;
it never truncates the user input or evicts higher-priority context.
A supported prior mismatch can influence actual model context but model
output quality/obedience is not proven by a fixed gateway fixture.

## Cognitive-owned intention

Reuse `cognitive_intentions` rather than introduce a new store. Existing
model-proposed, reflection-linked intentions retain their prior review
contract and are not injected by this increment. A typed
OFFER_SUPPORT_ON_NON_COMPLETION intention is derived from a newly admitted
Cognitive comparison of a non-completion report, with the exact comparison
and the new tentative directional expectation revision committed in that
same range. Its condition is a later independent non-completion report
of the same activity, while the pinned revision remains current.
No proactive next-act forecast, callback, timer, operation or autonomous
delivery. The optional intended approach is one small practical adjustment,
only when helpful. It is not a prediction about the user, a goal, permission,
or a claim that the user wants a particular intervention.

Intention expires after at most 24 hours, bounded by both expectation
validities; projection expires at most one hour after current report and
never after source/expectation/intention expiry. These pilot durations are
reviewable choices, not general approved thresholds. Selection is bounded
to one candidate by indexed exact context and current revision; invalid
candidate abstains, no fallback to older interpretations. A unique
response-lineage reservation makes each intention at most once admitted to
a generated response. Reservation is deliberately conservative, even if a
generated response is never delivered; no automatic recovery of a consumed
opportunity. This does NOT set already_asked, FULFILLED or prove actual
presentation of a suggestion. The existing model-proposed intentions
remain distinct. Actual reappraisal/approach effectiveness and attribution
are P2-F2, not this reservation. Explicit authenticated cancellation is
idempotent, event/outbox-linked, permitted while disabled, and suppresses
the exact intention even when already reserved; no deletion of ordinary
reports or expectation history. Bounded metadata-only inspection
distinguishes recorded status and reservation, without exposing content.

## Provenance and output fences

Migration 0068 extends existing intention and response-lineage tables.
Persist exact expectation revision, current outcome turn, optional
intention ID/kind/expiry and complete raw predictive source union; existing
control/version/generation and historical revision/expiry fence also pins
the observed preference/affect state. No forecast/resolution dependency
consumed here. Final validation uses the same SQL predicate at preparation,
commit and existing response-lineage controlled offer/ACK/replay/speech
checks: exact latest expectation and ingress association, current eligible
sources, no other same-occasion contradictory report or earlier duplicate,
current control and expiry, explicit boundary and intention condition,
source comparison and cancellation. No historical fallback. A response
using the projection is broad-fenced, never presentation-only. Its text
is excluded from summary/cognitive/recent reuse and response-derived
Dialogue intent source admission; model-supplied response intent candidates
are not admitted from the predictive prompt. Independent authenticated
input and ordinary deterministic interpretation remain eligible. No owner
mutation is authorized by the projection.

The existing Cognitive optional savepoint stages intention with comparison
and expectation revision. Their existing late validation/lease fences
cover those writes. Cancellation stages owner/event writes before the
Governance control NO KEY UPDATE lock. Output validates Memory SHARE then
control SHARE; existing owner writers serialize with control NO KEY UPDATE.
Supported admitted report ingress additionally serializes at the end of
the existing short input transaction: Memory revision UPDATE then control
NO KEY UPDATE, after all ordinary owner/event writes. Original ingress
pins are not refreshed; this closes the visibility race between a new
counterreport and a controlled projection check. Ordinary/disabled ingress
does not take the extra gate. No global lock or inference under locks;
cost and contention require target-host measurement. Existing P0 retry and
response historical fallback remain unchanged.

## Acceptance

New actual PostgreSQL regressions: history-dependent model prompt without
mood write, exact relevance/one reservation/no false delivery claim,
commit/offer/ACK/replay invalidation on revision, control, source,
counterreport and preference changes, cancellation before/after commit,
cold-start/budget/irrelevant suppression, expiry, no assistant-derived
evidence and cross-instance access. Overlap fixture observes actual
PostgreSQL blocking of a new report against the controlled-output gate.
Existing F1.3 four-polarity test now requires mismatch projection only
where relevant instead of the previous no-projection contract; its
forecast/evidence assertions remain. New candidate NOT RUN; static source
review, actual PostgreSQL CI and independent review required. A-PRED-1–5
new output owner paths not yet accepted. No story status promotion.

Remaining: general subjective-world projection, semantic comparability,
general model-proposed intention review and activation, cognitive
continuity GAP-06, strong surprise/established maturity, measured routing
and latency (P2-F3), Emotion-owned reappraisal/mood and actual delivered
approach attribution/learning (P2-F2), individual comparison correction,
retention/erasure and deployment policy. No proposed DEC-04 callbacks
adopted. US-117/118 remain partial, US-085 remains missing. Do not treat
synthetic tests or merge as calibrated behavioral acceptance.
