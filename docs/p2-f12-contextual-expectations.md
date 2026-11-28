# P2-F1.2 — Contextual expectations

Baseline `057efc8` (merged #145). Authority PRED-ROADMAP-01, v2.5.1 and
reconciled backlog. #145 verified 2,405 passed, 3 skipped
([contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46143062679),
[live acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46143062676)).
New candidate is NOT RUN.

## Initial vertical slice

Use existing watermarked Cognitive operation; no new model calls, scheduler,
per-turn job, pattern database or context projection. Cognitive owns immutable
`cognitive_expectation_revisions` in migration 0066. Keys are exact
(user_id, instance_id, reset_generation, context_key), revision version
monotonic within a key. The current record is the latest revision only.
Source IDs pin the full consumed evidence; occasion metadata records reported
date and polarity with the original user-report evidence class. The assessment
method is versioned `dated_activity_reports.v1`, epistemic status HYPOTHESIS,
not calibrated probability or verified completion.

The deliberately narrow first producer recognizes only a standalone
"On YYYY-MM-DD, I completed my daily walk." or
"On YYYY-MM-DD, I did not complete my daily walk.", with the named activity
substituted. Dates are user-reported, checked against trusted UTC reception
date; repeated reports of the same activity/date/polarity do not increase
evidence. Opposing reports for the same date are conflicting, not two
independent outcomes. No inference from mere engagement, assistant text,
absence, suggestions, global traits or model confidence. Different contexts
do not combine. The same producer uses earlier exact eligible raw reports
from the current revision, not the general model-visible reflection prompt.

## Provisional, deliberately conservative implementation decisions

These are pilot mechanics for review, not adopted universal maturity or
confidence thresholds. One distinct occasion yields EARLY; at least two
comparable non-conflicting reported occasions permit TENTATIVE only. Direction
reports which polarity has more distinct occasions, or MIXED on equal counts.
Any conflicting occasion makes the whole interpretation CONFLICTED / UNKNOWN.
Never mark established, predict a future act, claim verified outcomes or
automatically personalize. No global user reliability score. Repeated
counterevidence is preserved across revisions, not filtered to reinforce an
existing hypothesis. Incomplete evidence is not failure.

The pilot validity is 30 days from the oldest consumed reported date (end of
UTC day); a repeated report cannot renew expired evidence. The 64-source
review threshold is a bounded storage/inspection safeguard (range size <=16,
maximum pinned set <=79), not a sufficiency threshold. Beyond it create a
REVIEW_REQUIRED latest record; no fallback to older interpretations. An
expired or ineligible prior record is suppressed rather than automatically
rebuilding and discarding old counterevidence. Explicit owner withdrawal or
reset is currently required before an expired slot can be addressed, and a
withdrawn slot remains blocked until reset. This is intentionally narrower
than general drift-aware learning. The actual age, context taxonomy, repeated
occasion policy and owner-reviewed restart/revision policy require agreement.

## Ownership and fences

Preparation is part of the existing leased Cognitive range; only supported
raw text stamped while learning enabled is admitted. No automatic backfill.
Existing eligible Memory patterns are retained as Memory authority; broader
pattern/episode/summary/goal/feedback provenance is not yet a supported
predictive dependency under 0065, so no pattern is copied or treated as an
independent raw report. This is an explicit limitation of PRED-01 integration.

Stage Cognitive revision rows/event within the existing short owner
transaction before optional Memory pattern writes; revalidate the control,
generation, full raw-source set, Memory revision/expiry and own validity after
the pattern lock (avoids SHARE->UPDATE upgrade). Stale optional work rolls
back in a savepoint, preserving ordinary reflections; stale P0 claim rolls
back the entire commit. A competing next version raises a distinct retryable
conflict, leaving the range watermark unchanged and using P0's existing
bounded retry. No model I/O under locks. Stale control/source suppression
does not automatically catch up later. Owner revision metadata is immutable;
operation/context uniqueness and existing Cognitive job replay prevent
duplicate revisions. New revisions never revive reset generations.

Exact-context `read_expectation` is authorized diagnostic inspection only;
disabled/expired/withdrawn/review-required/ineligible current records return
None. Metadata-only history shows status without suppressed source text.
Authenticated local `withdraw_expectation` appends an immutable tombstone with
CAS, receipt and event/outbox; permitted while disabled. Correction of raw
evidence uses Memory's existing owner command. General semantic correction and
an explicit review-to-rebuild command remain open; no arbitrary model edits.
No remote management or new deployment consent decision.

## Acceptance and activation

Fixtures exercise actual Cognitive lease, range and write paths: two
histories, distinct contexts, early and conflicting evidence, independent
occasion deduplication, cross-range revision, disabled/pre-reset cutoff,
source correction/retraction/expiry, late disable/reset, immutable withdrawal,
competing revisions and stale lease. Synthetic inputs only. CI and independent
review required; current environment lacks PostgreSQL/Psycopg. A-PRED-1–5
actual owner admission remains NOT RUN. #145 tests cover the underlying
control and synthetic response path, not the new expectation owner.

No expectation is added to the ordinary prompt. Future prediction lifecycle
and projection (P2-F1.3/1.4) must pin the exact latest expectation revision,
not merely its source/control fence, and reject on new revision, expiry,
withdrawal, opt-out or reset at actual controlled output. Before reliance
after a pause, assess sufficiency and drift. No automatic Emotion/Relationship
changes or established expectation. Generalized contextual comparability,
Memory pattern integration, long-lived drift/expiry reconciliation, explicit
owner correction and GAP-06 continuity remain open. US-117/118 remain partial,
US-085 remains missing. No full phase acceptance.
