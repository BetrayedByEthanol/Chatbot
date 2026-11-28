# MVP-07B2.9 — Cross-domain evidence propagation (F06–F11)

Initial implementation on merged PR #142 (dbdfcfc). No service, model pass,
cache or generic provenance graph. The existing scoped PostgreSQL evidence
policy, owner transactions, P0 operations, immutable history and outbox remain.

| Finding | Implemented boundary |
| --- | --- |
| F06 Tool outcomes | Exact originating input checks before tool execution, result persistence, direct response commit/replay, and deferred offer/ack/replay. Already executed read-only work is not undone; stale results cannot be presented. |
| F07 Goal evidence | Effective evidence counts, lists and prompt context exclude withdrawn reports. Capture before the model read; keep goal approval and status unchanged. New evidence uses a late source fence. |
| F08 Memory extraction | Delayed deterministic and model extraction, review and approval enforce source eligibility. Memory correction admission uses a committed-source check, not generic raw recall which would reject the very unresolved conflict requiring owner review. Late Memory writes take an exclusive revision fence after owner locks. |
| F09 Research | Exact request/proposal approval evidence, existing document/version/passage pins, selection and bound variant eligibility. Unavailable research is marked and masked in the comparison; historical selection remains recorded. |
| F10 Background | Exact Goal opportunity input evidence pins (max 3) through model completion and pending semantic review; stale result rolls back with job/operation. ALIGN-01 final certification rechecks eligible task/run source evidence. Existing P0 claim/dispatch/completion and 0061 AGENT fences remain. |
| F11 Notifications | Source-linked intent admission, due and snoozed dispatch, effective inbox and inbound reply reads. Withdrawn due intent stops; withdrawn snoozed occurrence is cancelled. Unlinked explicit reminders and STOP/ACK remain supported. No new transport. |

## Contracts and limitations

Migration 0064 adds immutable bounded Goal proposal input pins and exact
eligibility functions. Legacy background proposals with unknown pins are
not accepted on semantic review; previously accepted goals are not silently
revoked. The Memory admission predicate deliberately excludes the explicit
report dispute gate and retains the committed revision and entity checks
from 0062; keep policy parity under review. Existing deterministic extraction
conflict/corroboration semantics remain the owner authority. A candidate
whose exact evidence is withdrawn is absent from effective review even if
its historical status remains CONFLICT or PENDING_REVIEW.

Goal evidence selection is scoped before validation. Some owner list/count
queries still evaluate all matching scoped evidence; no new DB-wide join
is intended. Measure long-lived same-persona history separately from unrelated
tenants. New background pins protect proposed work, not a retroactive change
to accepted goals. No automatic reevaluation of every historical critical
owner decision or revival of a stale READY certificate is introduced.
Existing tool result attempts may remain historical after a late revoke
between persistence and P0 completion; controlled consumption rechecks source.
Invalid tool work follows P0 bounded failure/retry; no new cancellation
protocol. Notification inbox suppression does not erase previously observed
content or existing AVAILABLE rows; already-seen users can STOP/ACK.
Dispatch count includes committed source invalidation transitions.

Tests are added to existing fixture modules for before/during/after tool
invalidation, direct delivery/replay, Goal evidence and operation rollback,
delayed/conflicting extraction, research approval withdrawal and notification
due/inbox/snooze. PostgreSQL execution and independent review remain required.
F06–F11 are not yet fully accepted. Broader semantic dependency discovery,
legacy unbound copies, durable reappraisal of accepted derived goals, exact
research selection approval chains beyond the present recorded pins, critical
epoch reassessment after certification, and end-to-end notification provider
copies are not solved by these changes.


Additional verification covers reviewed-model extraction before inference,
during inference and before approval; and exact durable Goal proposal pins
that force stale retirement before semantic review. The final Goal worker
check is ordered after resource release and before the live-clock operation
completion check, so a lock wait cannot extend a lease. No real PostgreSQL
execution has been performed by the author.
