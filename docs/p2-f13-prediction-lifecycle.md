# P2-F1.3 — Prediction lifecycle and selective comparison

Baseline `7baeacb` (merged #146), [Phase 0](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46149223791)
2,420 passed, 3 skipped and [live acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46149224696) PASS.
Authority: PRED-ROADMAP-01 §2/6.1/6.1.1/6.2, Architecture v2.5.1 and
reconciled backlog. New candidate NOT RUN. The existing dated-activity pilot
is deliberately narrower than general language understanding.

## 1. Pilot semantics and lifecycle

A forecast differs from a persistent expectation. A supported prior
TENTATIVE expectation with a clear direction can generate a bounded
prediction when an authenticated standalone plan is received:
"On YYYY-MM-DD, I plan to complete my daily walk." Same named activity,
today or next seven UTC dates, end of target UTC day as horizon. The
expectation must predate the plan and remain valid through the horizon;
no forecast for a target already reported or represented in the prior
expectation. A changed context within the same prepared range abstains.
No new model call, global reliability score, action authority, spontaneous
next-act anticipation, calibrated probability or established-maturity claim.
These pilot horizon and comparability rules are implementation decisions,
not adopted general policy.

| Lifecycle | Contract |
| --- | --- |
| Formation | Immutable forecast pins exact expectation ID/version, full raw support/counterevidence, plan input, target/date, control version, generation, creation and deadline. At most one forecast for the context/date/generation. |
| Outcome ingress | Matching supported raw report binds an already-visible forecast and prior expectation in the existing authenticated input-admission INSERT. Null binding remains null. No retroactive matching by delayed cognition. |
| Resolution | Existing leased Cognitive range can resolve once before deadline, using 1–16 distinct exact bound reports, current source/control and prior expectation. Single polarity yields HIT/MISS relative to forecast; conflicting reports yield INCONCLUSIVE. Unbound same-target report inside horizon blocks resolution, including races. |
| Expiry | Immutable horizon makes an unresolved forecast terminal EXPIRED / INCONCLUSIVE / UNKNOWN on the server clock. No artificial miss, synthetic outcome, per-forecast timer, additional job or expiry event. Expiry is derived from the durable deadline, not a second persisted resolution. A report not processed before the horizon is inconclusive, even if received in time; delayed recovery is intentionally not claimed. |
| Invalidation | Control change, reset, withdrawal, prior revision/expiry or source withdrawal suppresses pending forecasts. A new conflicting report suppresses earlier HIT/MISS certainty without a second resolution. |
| History | A normal outcome-driven expectation revision may coexist with a diagnostic HISTORICAL resolution pinned to the prior version, only while exact evidence and current latest expectation remain eligible. It is not a current forecast or permission for reappraisal. |

Ordinary expectation revision is the existing Cognitive owner path using
the same independently reported outcomes; forecast labels do not become
new evidence or additional observations. No duplicate corroboration,
negative update for silence, or automatic Goal/Emotion/Relationship effects.
When learning is disabled no new forecast, comparison, or learned resolution
is admitted. Any intervening control version change invalidates pending and
recorded forecast/comparison use, including disable/re-enable; a fresh
expectation revision in the current control version is required to form new
forecasts. This is a conservative pause/drift policy, stricter than mere
generation equality. No disabled/pre-enable catch-up or old target revival.

## 2. Selective comparison

The ordinary full Context Builder calls a bounded host diagnostic only for
the normal response path (mandatory goal/procedure/fact/correction routes
remain unchanged). No diagnostic content is included in model messages or
controlled output, and no response lineage fence is implicitly activated.

| Route | Meaning |
| --- | --- |
| COMPARE_NOW | Exact standalone supported dated report: scoped stamp lookup, applicable prior expectation pin and eligibility; no applicable evidence is UNKNOWN. |
| DEFER | Bounded ambiguous completion/correction cue; no predictive query or invented comparison. Existing watermarked cognition can process supported evidence, but generalized semantic resolution remains open. |
| SKIP | Unrelated input; no predictive SQL, hint or persisted routing state. |

EXPECTED/UNEXPECTED compare a new independent report to an unambiguous
prior TENTATIVE direction. CONTRADICTORY requires a conflicting report
about the same occasion, not mere unexpected behavior. Duplicates,
unsupported/withdrawn/ineligible sources, early/mixed expectations and
missing prior pins abstain with UNKNOWN. Comparison is not an emotional
reaction or a statement of verified task completion.

The Cognitive worker records eligible comparison candidates with exact
expectation and evidence pins; uniqueness by expectation revision, occasion
and polarity (also unique source turn) prevents repeated reappraisal
candidates for the same evidence. Actual reappraisal/Emotion delivery
deduplication and consumer admission are P2-F1.4/F2 gates. A later contrary
report suppresses old EXPECTED/UNEXPECTED certainty; no fallback to invalid
older interpretations. No durable SKIP/DEFER state or evaluation service.
P2-F3 measures the integrated routing quality, retrieval cost, missed
opportunities and deferred recovery on held-out scenarios.

## 3. Persistence, transaction and owner boundaries

Migration 0067 adds Cognitive-owned immutable predictions, resolution rows,
comparison candidates and withdrawal tombstones. Scoped FKs connect exact
expectation, operation, input and owner records. Existing predictive input
admissions are extended with nullable exact report metadata/pins; no backfill
and no changes to ordinary stamp INSERT for unrelated input. Bounded indexes
support exact scope/context/date lookup. Ingress only binds evidence, does
not resolve forecasts, modify expectations or grant action authority.

Use existing watermarked Cognitive operation/lease, no new scheduler or
inference. Stage expectation/prediction/comparison/resolution rows and event
before late dependency locks. Existing Memory pattern admission may already
hold Memory UPDATE. Acquire historical revision SHARE, then scoped
Governance control NO KEY UPDATE (compatible with ingress FK KEY SHARE);
learned owner writers and authenticated withdrawal serialize their late
validation through this gate. Governance changes retain selection UPDATE.
No owner/event writes after acquiring the shared Memory fence; recheck
exact prior revision excluding only this operation's own new expectation
revision, all pinned raw sources, latest report membership, control and
deadline. Existing final live claim check remains. Deterministic sorted
writes and unique next revisions/terminal resolution protect competing
workers. Stale optional predictive work rolls back in a savepoint and
independent expectation/pattern processing is retried without it; stale
expectation and claim behavior remains as before. No locks span inference.

A report admitted concurrently without a visible forecast cannot acquire
a later pin; target-occasion checks suppress resulting false hindsight
even if the ingress committed after the forecast's last check. Newly
conflicting reports can suppress previously recorded diagnostic results.
No generalized semantic correction inference.

## 4. Inspection and withdrawal

`read_prediction` and `read_recorded_comparison` are exact scoped
authenticated local diagnostics, not model input or operation grants.
`inspect_prediction_history` returns bounded metadata-only records and
governance/recorded lifecycle status; exact eligibility is checked by the
owner read, not claimed by metadata. `withdraw_prediction` appends an
authenticated immutable tombstone with receipt and event/outbox, permitted
while disabled; it does not erase the ordinary expectation or reports.
Expectation withdrawal and Memory evidence correction remain independent.
No new remote management/retention/deployment policy or physical deletion.

## 5. Verification and unresolved gates

New regressions cover real ingress and Cognitive leases, current prior
revision, all HIT/MISS polarities, conflicting and duplicate reports,
cold-start and irrelevant/ambiguous routing, source/control withdrawal,
no retrospective binding, explicit owner withdrawal, competing operations,
expired claim and time-only expiry (historical fixture with a shortened
already elapsed horizon). No PostgreSQL execution in the author
environment; CI and independent review required. A-PRED-1–5 new owner
admission NOT RUN. No story status promotion. General semantic comparability,
derived Memory patterns, broader prediction targets, late outcome recovery,
contextual drift/expiry reconciliation, individual comparison correction,
and measured foreground routing quality remain open. No expectation,
forecast or recorded comparison is consumed in model context yet; P2-F1.4
must introduce exact current revision, source/control, horizon and output
fences before actual presentation or appraisal. GAP-06 continuity,
US-117/118 remain partial; US-085 remains missing. No full phase acceptance.
