# P2-F1.1 — Predictive learning admission contract

Baseline `4b66a3e` (merged #144). Authority: PRED-ROADMAP-01 §6.1 and §6.1.1,
Architecture v2.5.1 and reconciled backlog. Candidate only, no real-user
predictive producer, no learned-content table, no new model inference.

## Control and causal boundary

Authorization/Governance owns `predictive_learning_controls` at
(user_id, instance_id). Missing record means disabled, version 0, reset
generation 0. Selected foreground authenticated user commands set enabled
or reset via existing receipts, expected control version and selection
generation. Effective changes publish a scoped authenticated event and
outbox row atomically; same-state command is a receipt-only no-op. Reset
always increments generation/version, retains enabled state and sets
reset_boundary_ref to its durable Governance event. Read permits owned
instances, with current session checks for application actors. Management
writes intentionally require the trusted local authenticated boundary;
conversation-only 07A sessions do not gain management authority. No UI or
remote activation. Deployment consent/retention policy remains DEC-03.

The existing conversation journal has one event stream per conversation,
not a globally ordered event sequence. The reset cutoff is implemented
through the existing selection gate: Governance takes selection UPDATE;
all authenticated text ingress already takes SHARE. A live text input
committed while enabled receives an immutable exact input-event stamp
with current generation and control revision. Disabled, pre-enable,
pre-reset and legacy inputs are not backfilled. A reset excludes all
older stamps regardless of conversation, and re-enable does not stamp
previously disabled input. No clock ordering or global event allocator.
Ordinary Memory is unaffected. Existing stamp creation revision need not
equal current control revision after a pause; generation and eligibility
must match. Prepared work must match the current revision exactly.

## Initial admitted source class

Only raw authenticated text ingress with exact scoped turn/input-event
identity, current-generation stamp, event type/trust and existing
dialogue/Memory current-source predicate. The 1..128 distinct source set
is host-selected, not model supplied. A user report is not verified task
completion. Missing, unknown, mixed invalid, foreign, voice, assistant,
summary, pattern, reflection, task outcome, feedback or other derived
dependencies are unsupported; do not strip them from a candidate in order
to admit it. No model confidence or learned interpretation is independent
evidence. Later owner paths must add explicit source classes, transitive
pins, comparability/sufficiency and counterevidence before activation.

## Transactions and outputs

`capture_predictive_fence` reads current control, historical revision/next
expiry and exact inputs; returns None on ineligibility. Owner commit
must acquire existing selected-generation/lease and owner locks first,
then validate Memory SHARE and Governance control SHARE via
`require_predictive_fence`. Existing operations keep their live lease
check after blocking locks; this helper does not schedule or claim work.
No inference/tool calls under locks. Control transitions take selection
UPDATE and write only their own row/events/receipts, not Memory or learned
content. No late control row lock is required during default-off ingress.

Conversation's optional host-only predictive fence is stored in existing
immutable response lineage with the historical fence. Final commit,
controlled offer, ACK/replay and existing speech admission check it.
Predictive replies remain broad, never presentation-only; derived
assistant text is omitted from recent/summary/cognitive reuse and
response-derived Dialogue signals are ineligible. Owner-changing
entity/Goal/guidance responses cannot use this optional contribution.
No production path populates it yet: synthetic tests attach a host
fence to exercise actual output boundaries. A learned path must prove
all of its actual dependencies, owner effects and re-enable sufficiency
before it may use this extension.

## Acceptance status

| Gate | Candidate evidence | Status |
| --- | --- | --- |
| A-PRED-1 | Exact scoped raw text admission, unsupported/foreign and mixed source exclusion | NOT RUN; later source classes and owner content deferred |
| A-PRED-2 | Current source policy, correction/retraction/expiry while paused, no old-generation revival | NOT RUN |
| A-PRED-3 | CAS, receipt replay, version fences at real response commit/offer/ACK/replay | NOT RUN; actual future owner operation/lease integration required |
| A-PRED-4 | Default off, authenticated commands, durable reset, no disabled-period catch-up | NOT RUN; learned-state inspection/correction requires owner content |
| A-PRED-5 | Ordinary facts and explicit preference continue under independent contracts | NOT RUN; no prediction producer enabled |

Actual PostgreSQL CI required. Source inspection only by author.
Control inspection is implemented; Inspect/Correct/WithdrawLearnedState
will be owner-specific once records exist, not an empty generic store.
No general cognitive continuity/GAP-06 completion claim; unresolved
question/competing hypothesis continuity remains to be reviewed within
P2-F1.1/1.6. No maturity thresholds, prediction lifecycle, approach
learning, retention/physical erasure, or external provider policy selected.
US-117/118 remain partial, US-085 remains missing. No acceptance promotion.
