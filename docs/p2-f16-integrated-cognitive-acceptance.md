# P2-F1.6 — Integrated cognitive acceptance candidate

Baseline `c20240c` (merged #149). [Phase 0](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46165139597):
2,486 passed, 3 skipped, 17m 24s; [live acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46165139592) PASS.
Authority: PRED-ROADMAP-01, v2.5.1 and roadmap GAP-06/13/20.
This is a technical integration candidate, **NOT RUN / NOT ACCEPTED**.
No new model, worker, background trigger, learned policy or phase gate.

## 1. Review of implemented increments

| Increment | Existing actual producer and consumer | Residual |
| --- | --- | --- |
| F1.1 | Authenticated enable/reset, raw ingress admission, exact source/control fences | General cognitive continuity not solved by governance; broader retention/remote policy open. |
| F1.2 | Scoped versioned contextual expectations, independent dated occasions, counterevidence and maturity | Narrow deterministic pilot; generalized semantic comparability/maturity unresolved. |
| F1.3 | Prospective forecasts, ingress-bound later reports, terminal resolution and selective comparison | Broader targets, delayed outcome processing, general comparison/reappraisal open. |
| F1.4 | Actual context projection and typed intention reservation with controlled-output revision fences | Reservation is not delivered approach/fulfilled intention; no strong surprise or persistent mood. |
| F1.5 | Current recall-gap states, exact feedback, significant experience metadata and bounded Cognitive attention | Gap attention read not a cross-range reflection queue; implicit salience and broad feedback open. |

The merged baseline passing individual contracts does not by itself prove
integrated correctness. Existing expectation revisions and typed intentions
have real cross-conversation lifecycle. Generic Cognitive questions,
hypotheses and intentions remain lower-trust proposals. The existing
normalized-content link identifies repeated interpretations, but the
previous reflection is NOT a model input or independent corroboration.
No arbitrary link is allowed to activate an intention or prove a conclusion.

The integrated review found that `list_cognitive_proposals` could expose
the ID of an invalidated earlier related reflection. Mask that reference
unless the exact scoped prior reflection is still PROPOSED and its complete
job lineage is eligible. Keep the historical stored link and the newer
reflection's independent evidence unchanged. No fallback or recursive
dependency: this is an association, not evidence consumed by the model.
General prior-hypothesis retrieval, competing interpretation resolution,
question closure and model-proposed intention review remain GAP-06.

## 2. Bounded session review

`session_review.review_session_range` is an authenticated local Evaluation
read for one exact scoped conversation and explicit contiguous closed
sequence range (1–64 turns). It does not interpret application credential
expiry as a session boundary. Pending input or a still-deliverable GENERATED
response makes the requested range not ready. Revoked/invalidated output is
not evidence of delivery. The caller chooses the review range; no
automatic session-end model call or automatic background dispatch.

Use a single read-only repeatable-read transaction for owner snapshots.
Return summary/cognitive coverage flags, eligible turn count, typed
owner-reference findings and bounded output (1–128 findings, truncation
explicit). No user or assistant prose, unverified ratings, arbitrary
reflection text, instructions, or new authoritative state. The snapshot
is diagnostic only, never a reusable permission/fence. Owner commands
must independently revalidate if a human later chooses to act. Server
clock expiry still applies at each predicate; no freshness guarantee after
return.

| Finding | Current evidence and meaning |
| --- | --- |
| EXPERIENCE | Eligible explicit user-marked experience metadata; ACTIVE/AGED attention, not verified event. |
| RECALL_GAP | Exact latest non-budget MetaMemory observation for that route/topic, before eligibility. OPEN/AWAITING_CLARIFICATION/REVIEW_REQUIRED; stale request excluded, later cross-session resolution not reopened. |
| RETRIEVAL_FEEDBACK | Existing exact authenticated adjacent report about delivered recall, with both request and feedback in range and eligible. RELEVANT/IRRELEVANT/INCORRECT/MISSED are user reports, not proof of answer truth. |
| CORRECTION_SIGNAL | Current deterministic Dialogue correction proposal, not proof of a model error or a resolved repair. |
| REFLECTION_PROPOSAL | Current source-linked QUESTION/HYPOTHESIS/SELF_EVALUATION proposal. A model self-rating is not user feedback. |
| GOAL_REPORT | Exact eligible Goal-owned user report, linked to report version and current goal version. Direct active root with exact authenticated creation and approval sources is APPROVED_GOAL_REPORT; others are GOAL_REVIEW_REQUIRED. No goal achievement or semantic alignment inferred. |

Goal read uses existing `goal_evidence_current` and
`workflow_source_current` source barriers; it does not certify delegated
ancestry, tasks, strategy or procedure grants (existing ALIGN-01 owner
review remains authoritative for those). Migration 0070 adds only source
lookup indexes on existing goal evidence and reflections. No new store.
Review of goal evidence with no current approval is a review requirement,
not permission to pause, change or remove the approved objective.

Aggregate feedback labels summarize only eligible reported retrieval
feedback in the selected range: NO_FEEDBACK, RELEVANCE_REPORTED,
DISSATISFACTION_REPORTED, MIXED_REPORTED. Silence, neutral endings,
length, missing evaluation, incomplete cognition and model self-rating
are not dissatisfaction or success. Explicit dissatisfaction about recall
is not global assessment of the persona. Current adverse feedback can
mark the latest gap REVIEW_REQUIRED even when its feedback turn is outside
this selected review range; individual feedback findings and aggregate
feedback remain range-exact. No behavioral adjustment from these labels.

The review only reads owner findings; no duplicate evaluation persistence.
General session-boundary scheduling, user-approved adaptation and
periodic long-term review need later explicit contracts.

## 3. Integrated regression candidate

`tests/test_p2f16_integrated_cognition.py` combines real ingress,
Cognitive leases, owner commits, response generation/ACK and source
invalidation across conversations, using independent DB connections and
fixed model gateways. It tests:

- Prior tentative expectation -> prospective forecast -> independently
  reported mismatch -> actual fenced model context/delivery -> Cognitive
  resolution and ordinary expectation revision. Learned assistant text
  excluded from Cognitive evidence, no commitment counted as outcome.
- Crash after durable Cognitive commit and before P0 completion;
  expire only the fixture lease, reclaim, replay without a model call,
  one terminal resolution and one committed proposal.
- Same-user other-persona isolation, foreign-user denial, later conditional
  support intention, relevance and reservation not fulfilment.
- Memory source withdrawal, stale controlled response rejection, control
  disable/reset/re-enable and cold-start new evidence; independent marked
  experience and unrelated question survive. No automatic goal creation.
- Repeated cross-session model hypothesis link, unchanged trust/confidence,
  stale prior association masked without deleting history.
- Neutral ending and missing feedback, pending/generated review rejection,
  bounded review, actual delivered positive/negative retrieval feedback,
  feedback expiry, correction signal eligibility, significant experience,
  later cross-session recall resolution, direct approved Goal report and
  approval-source retraction. No automatic objective changes.

Fixture-only cleanup hardening: when a test abandons an active Cognitive
claim, P0 cancellation is cooperative. Expire only cancelled claims in the
fixture's own scope before using the actual reaper. Preserve reservations,
pool counters and P0 production behavior. Earlier PR #149 failure
demonstrated why cancel request alone is insufficient.

## 4. Gap disposition and acceptance

| Checkpoint | Candidate evidence | Status |
| --- | --- | --- |
| A-PRED-1/2 exact sources, comparability, correction | Cross-session owner lifecycle and independent evidence regression | NOT RUN |
| A-PRED-3 admission, delivery, recovery | P0 recovery and exact source/response checks; prior F1.1–F1.5 suite remains | NOT RUN |
| A-PRED-4/5 control and cold start | Disabled/reset/re-enable and scoped independence | NOT RUN |
| GAP-06 | Repeated association validity; real expectation/typed intention continuity | PARTIAL / generalized hypothesis & question lifecycle open |
| GAP-13 | Exact delivered retrieval feedback and tentative correction findings | PARTIAL / actual approved mistake adjustment, held-out recurrence and rollback open |
| GAP-20 | Bounded read of existing owner evidence; no silent feedback inference | PARTIAL / broader session review, semantic goal alignment, behavioral adaptation and periodic review open |

Static source/diff review only. Actual PostgreSQL suite, live integrated
acceptance, independent review and operator acceptance pending. Do not
promote story statuses, claim calibrated predictive or behavioral quality,
or close Phase 2 based on merge. P2-F2 owns actual Emotion reappraisal and
approach/outcome learning; P2-F3 measures relevance and performance. P6-F2/F4
broader evaluation remain separate. US-117/118 remain partial; US-085 and
DEC-03 governance/erasure remain open. No DEC-04 callbacks adopted.
