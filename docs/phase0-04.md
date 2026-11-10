# P0-04 — Durable Operation Scheduler (Architecture v2.5)

**Implementation status:** Phase 0 scheduler foundation. This slice does
not enable persona autonomy or execute network/model/tool calls by itself.

## Owners and transaction boundary

The Execution System owns operations, worker admission/claims, concurrency
groups, resource reservations, retries, checkpoints, cancellation and
unknown-outcome quarantine. Domain owners remain responsible for task/goal
success and approval of produced state changes.

All mutating entrypoints require a psycopg connection with
`autocommit=True` and `transaction_status=IDLE`; each function owns
its complete short PostgreSQL transaction. They never hold DB locks during
model inference, a tool call or provider I/O. Each meaningful transition
appends an event plus outbox record in the same transaction. Worker heartbeat
lease extensions are infrastructure bookkeeping, not independent domain
events.

This package intentionally uses **no broker or global mutable scheduler
state**. PostgreSQL READ COMMITTED, row locking, fencing generations,
idempotency and a per-operation event stream provide durability.

## Contracts

- `EnqueueOperation`: the authenticated primary-user command carries a
  stable operation UUID and scoped idempotency key, selected instance and
  expected execution generation, operation type, origin, policy, priority,
  capabilities, resources, dependencies, optional entity versions, deadline,
  timeout and retry limit. Commands with task_id are explicitly rejected
  until the Task domain exposes the shared versioned dispatch gate.
- `enqueue_operation`: verifies user/instance ownership and selected
  generation, deduplicates a full request fingerprint, validates dependency
  scope, resource feasibility, input state version, and allowed origin.
  The command, queued record, domain event, outbox and receipt commit
  atomically. Ownership is checked before scoped receipt replay, but mutable
  input versions are checked only on new admission. Committed retries return
  their saved result even when the instance version has since changed.
  Replays do not re-authorize work after a persona switch.
- `claim_operation`: rechecks policy, selected generation, Background
  epoch, required capabilities, completed dependencies, declared persona
  instance input version, deadline, exclusive concurrency group and
  available resources. Candidate ordering is paginated using a priority
  keyset; scan_limit controls each page size, not the total candidates
  visited. Capability and dependency mismatches are filtered before the
  page limit. Resource- or mode-blocked work cannot permanently starve
  later eligible work. Final admission happens under PostgreSQL locks.
  Claims use claim_generation,
  worker_id, lease_until and attempt_count, with resource reservations
  made in the same transaction as the RUNNING transition.
- `heartbeat_operation`, `checkpoint_operation`,
  `complete_operation`, `fail_operation`: require the current claim,
  live lease, absolute timeout, deadline, current persona selection, epoch
  where required, and declared input versions before accepting effects.
  Completion means the operation ended, **not** the task/goal succeeded.
- `cancel_operation`: authenticated user can cancel even after the
  originating persona becomes inactive. A queued operation becomes
  CANCELLED; a running operation gets CANCEL_REQUESTED and is prevented
  from committing normal results. `acknowledge_cancel` releases resources.
- `reap_stale_operations`: reconciles expired/invalidated attempts
  without trusting an expired lease as proof an external effect failed.
  Retryable internal work enters RETRY_WAIT (bounded attempts); exhausted
  or expired-deadline work becomes FAILED. Background ephemeral work is
  CANCELLED on return/switch; safely resumable work is SUSPENDED.
- `resume_suspended`: explicit authenticated re-admission under the
  newly selected generation; rejects undispatched work with unresolved
  owner dependency versions, which the domain must revalidate first.
- `mark_external_dispatched`: durable intent marker immediately before
  the actual external call. External execution is **not implemented here**.
  If interrupted after dispatch, status becomes OUTCOME_UNKNOWN and
  exclusive resource/concurrency reservations are retained. No automatic
  retry. Only trusted operator reconciliation with provider evidence
  through `reconcile_unknown` releases the resource and sets outcome.

## Policy admission and background barrier

- P0 Foreground async: high-priority interactive work; resource pools
  expose `p0_reserved_units` that P1/P2 cannot consume.
- P1 Foreground async: user-requested or approved preparation continues
  between Foreground and Background while the same persona is selected.
- P1 Background ephemeral: selected persona must be in Background;
  result invalidates on presence return or persona switch.
- P2 Background resumable: selected persona must be in Background and
  `background_ready_epochs` must contain the **current** epoch. There
  is intentionally no public function in P0-04 that marks an epoch READY.
  Only the future strict Phase 4 summarize/evaluate/critical-commit
  barrier may do that. A persona switch invalidates selection generation.
- INACTIVE instances cannot claim, advance or commit persona work.
  A global platform reminder dispatcher is separate from this
  persona-owned scheduler.

Concurrency-group exclusivity uses a row with a unique
`(user_id, instance_id, group_key)` and reservations bound to one
operation claim. Pool `active_units` is updated with reservations and
releases atomically. A quarantined external outcome intentionally holds
its reservations until reconciled. This is a safety choice, not a leak
to be cleaned up blindly.

## Authorization, provenance and deliberate limits

The current admission entrypoint accepts a `TrustedActor` from
authenticated primary-user ingress. It cannot create agent-originated
operations. A future authenticated domain-internal producer must have a
separate authorization contract; an untrusted model or channel payload
cannot self-label as user activity.

Only `input_versions.persona_instance` is currently understood and
enforced. Other domain versions require their owning domains before
admission/commit. Task-owned operations are not accepted. External calls
still require the future Model/Tool Gateway capability/permission policy;
the current dispatch marker is *not* permission to perform an external
effect. No actual worker runtime loop, provider-side idempotency, crash
reconciliation integration, or capability executor is installed.

This is intentionally narrower than Phase 4 autonomous work, including
the strict Background barrier, cancellation propagation through task
graphs, provider reconciliation and platform notification routing.

## Migration and validation

Apply the numbered PostgreSQL scripts in order through
`migrations/0004_operations.sql`. The existing CI fixture applies all
SQL scripts against disposable PostgreSQL 16.

```bash
python -m pip install -e ".[test]"
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0
python -m pytest -q
```

Tests cover per-user/persona isolation, idempotent admission/cancellation,
concurrent worker claims, capacity and P0 reservation, capability and
dependency checks, concurrency groups, cancellation races, fenced worker
writes after switching/return, lease expiry/timeout/deadline, retry
exhaustion, epoch preparation gating, external outcome quarantine,
evidence-based reconciliation, and transactional event/outbox changes.

## Decision register

- **EXEC-P04-01:** PostgreSQL-only operations and resource reservations;
  no new scheduler broker or worker service.
- **EXEC-P04-02:** P2 always requires an explicit current Background
  preparation record. P0-04 cannot create such a record.
- **EXEC-P04-03:** Unknown external-effect outcomes retain exclusive
  reservations until audited provider reconciliation.
- **EXEC-P04-04:** Only versioned persona-instance dependencies are
  validated in this slice; task/domain gates are deferred, not guessed.
- **EXEC-P04-05:** User-origin operation requests cannot forge autonomous
  cognitive or goal-origin authority.


Recovery sweep also processes stale queued and retry-wait work after a
persona switch, Background epoch invalidation or expired deadline. A queued
operation is not left indefinitely waiting for authority that no longer exists.

A stale declared persona-instance input version moves pending/running
undispatched work to BLOCKED during recovery. It cannot keep consuming
resources or silently retry against an obsolete state snapshot. A domain
owner must revalidate and issue a new command before proceeding.

## Review hardening — ephemeral epoch and completion expiry

Background ephemeral work binds its target epoch when enqueued. Background
admission uses the current epoch; Foreground admission targets the immediately
next Background epoch. A queued operation cannot execute in a subsequent
Background cycle after its original one finishes. Recovery cancels expired
pending work; a receipt replay never changes its epoch.

Completion revalidates the full claim after acquiring selection and pool
locks. Its final UPDATE independently requires a live lease, absolute timeout
and deadline according to PostgreSQL time. Rejection rolls back the resource
release, state change, completion event and outbox atomically; recovery then
handles the expired claim.

Pending ephemeral rows from earlier P0-04 builds whose epoch is NULL
are treated as invalid by recovery and cancelled; they cannot remain
indefinitely queued after the admission contract changes.

## P0-04 heartbeat lock-wait fence

Heartbeat first validates the worker claim, then obtains persona selection
and input-version locks. Since those locks may block without altering
selection generation or mode, heartbeat **revalidates the original claim
after they are acquired**. Its lease-extension UPDATE additionally checks
status, worker ID, claim generation, selection generation, existing lease,
absolute execution timeout and deadline against PostgreSQL wall-clock time.
An expired heartbeat raises `StaleOperation` and cannot revive the attempt;
its reservation remains with the expired attempt until the recovery sweep.

## External dispatch lock-wait fence

`mark_external_dispatched` checks the current claim after obtaining
selection and input-version locks. Its state-transition SQL also requires a
live lease, absolute execution timeout, deadline, worker and claim generation,
and a PREPARED external effect. If a lock wait outlives the lease, the
transaction rolls back without a DISPATCHED marker, event or outbox entry.
The recovery sweep can then safely retry undispatched work instead of
quarantining a falsely dispatched operation as OUTCOME_UNKNOWN.

## Checkpoint lock-wait expiry fence

`checkpoint_operation` validates the original claim both before and after
acquiring authority and input-version locks, which may block without revoking
the persona generation. The final PostgreSQL UPDATE independently requires
the same active worker, claim generation, unexpired lease, absolute timeout,
and deadline. An expired checkpoint attempt raises `StaleOperation`, rolling
back its checkpoint change, event, and outbox atomically. A previously
committed checkpoint remains unchanged for safe retry or suspension.
