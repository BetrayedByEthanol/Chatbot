# P4-05a — Cooperative Background suspension

Architecture authority: Persistent Persona Agent Architecture v2.5.1, and
`docs/phase4-plan.md`. This is the first P4-05 vertical slice. It does not
introduce another scheduler, restart protocol or external-effect executor.

## Contract

* Foreground return and persona switch first revoke Background epoch/generation
  through the existing short lifecycle transaction. They do **not** acquire
  operation row locks or wait for any worker/tool.
* P0 observes the committed revocation via ordinary recovery polling
  (`reap_stale_operations`), or a running P2 executor invokes
  `request_background_suspension` at a safe boundary.
* Only previously claimed P2 `BACKGROUND_RESUMABLE` operations with
  `effect_status=PREPARED` can transition
  `RUNNING -> SUSPEND_REQUESTED` here. Requesting suspension preserves the
  lease, claim generation, checkpoint reference and resource reservations.
  Cancellation wins if a user has already requested cancellation.
* The same live fenced worker calls `acknowledge_suspend` after stopping
  safely. It atomically releases reservations and records
  `SUSPEND_REQUESTED -> SUSPENDED` with an event/outbox entry. It does not
  write a success result or imply that the task/procedure step completed.
* If the worker hangs or crashes, the existing P0 reaper waits for lease,
  timeout or hard deadline expiration, then suspends/reconciles according to
  existing effect-status and retry rules. Old claims cannot commit late.
* The registered `text.statistics.v1` executor is synchronous; P4-05a checks
  cancellation/preemption before and after the tool. It does **not** claim
  mid-call interruption or a recoverable checkpoint for that tool.
* Existing single-epoch authorization remains unchanged. Suspended P2 work is
  **not** resumed on another epoch by `resume_suspended`: Phase P4-05b/c
  define checkpoint schemas and renewed, owner-validated authorization.

## Acceptance

Real PostgreSQL tests cover Foreground responsiveness, idempotent suspension
requests, authority scope, worker acknowledgement, resource release, reaper
behavior while a lease is live, stale completion denial, and discarded read-only
results after user return. P3 Foreground cancellation behavior remains intact.
No claim is made that arbitrary external tools are cooperatively interruptible.
