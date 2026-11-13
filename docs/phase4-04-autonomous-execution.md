# P4-04 — Authorized bounded autonomous selection and execution

Authority: Persistent Persona Agent Architecture v2.5.1, `docs/phase4-plan.md`,
and the merged P4-03 ALIGN-01 task-scoped certificate.

## Implemented vertical slice

**Only** the already-registered read-only `text.statistics.v1` tool can execute
autonomously. A user must explicitly authorize an exact ProcedureRun / AGENT
step / Task version for **one next Background epoch** with a separate,
literal procedure approval turn. This does not provide a broad authorization
to model-generated tools or to future epochs. P3 Foreground approvals remain
distinct and cannot be promoted to P2.

1. Foreground: the authenticated user sends
   `AuthorizeBackgroundAgentStepCommand` with task/run/step versions, exact
   `operation_id`, `expected_background_epoch`, bounded `max_words`,
   supporting source turn, and separately approved confirmation. The Procedure
   owner stages one `AUTHORIZED` grant with a journal/outbox event; it creates
   **no operation** and executes no tool.
2. Background: the existing `SUMMARIZE → EVALUATE → CRITICAL_PENDING →
   ALIGNMENT_PENDING → READY` preparation finishes. The producer
   `background_autonomy.dispatch_one_background_step` scans previously staged
   grants ordered by validated P4-03 priority and deadline, accepting only an
   `ELIGIBLE` task with correct generation and epoch.
3. P0: `enqueue_operation` admits only `TrustedPlatformActor` from
   `background.autonomous.dispatch`, `GOAL/P2/BACKGROUND_RESUMABLE`,
   `READ_ONLY`, the exact registered tool, canonical resources/capability,
   pinned Task/Run/Step versions and operation ID, and the existing user-
   approved stage. It retains P0 idempotency, event/outbox, P0/P1 reserves,
   resource/group locks, work claiming and lease handling. A per-persona
   transaction advisory lock serializes the **maximum two currently valid P2**
   admissions per epoch; one step is selected per producer tick. Persistent
   polling timestamps rotate among READY personas and a per-epoch cursor
   prevents an expired grant from starving later approved steps.
4. The existing Procedure-owned `run_one_agent_step` invokes the registered
   read-only tool **outside any database transaction**, revalidating P0 claim,
   current epoch, certificate, Goal ancestry, Task, Run, Step and permission
   immediately before dispatch and at completion. The existing
   `reconcile_agent_step` promotes only a verified fenced result. A tool
   result does **not** independently confirm Task or Goal attainment.

A small driver `python -m persona_agent.background_autonomous_worker` calls
the existing P0 claim/worker/reconciler; it does not create a second scheduler.
Use `--once` for a bounded invocation. Existing Background preparation,
summary and cognitive workers must also be deployed for READY to become
reachable; resource pools must be provisioned by the trusted operator.

## Revocation and recovery invariants

- Returning to Foreground (or switching persona) invalidates the epoch. P2
  admission pins the exact epoch; P0 and the Procedure gate recheck it at
  enqueue, claim, dispatch and completion. A later Background epoch cannot
  revive a stale grant or queued P2 operation.
- Foreground P1 authorizations (`approved_background_epoch IS NULL`) cannot
  be dispatched as P2. Background P2 grants cannot be enqueued as P1.
- A user-cancelled, suspended, mismatched-priority, deadline-expired or stale
  task/run/step/approval does not execute; unapproved tasks and model
  proposals have no operation admission API.
- Crash after user approval but before P0 enqueue leaves a durable grant; the
  next producer poll resumes admission idempotently. The P0-owned
  `reap_invalid_background_queue` journals `BLOCKED` for stale queued/retry
  operations whose Task/Run/Step/epoch authority is permanently revoked.
  Its authoritative slot-budget query excludes such dead queued reservations
  but continues counting RUNNING/CANCEL_REQUESTED/SUSPEND_REQUESTED claims.
  A generic P0 recovery pass may first mark never-claimed queued work
  `SUSPENDED`; the P4 reaper safely converts only zero-claim,
  `PREPARED` suspended records to `BLOCKED`, never claimed or
  externally dispatched work.
  Fresh explicit user approval can supersede a previously blocked Background
  grant; historical completed results must be reconciled first.
- Crash after a P0-confirmed read-only completion but before Procedure result
  reconciliation is repaired by a bounded terminal-result scan. That
  historical `COMPLETED` proof may be accepted after Foreground return while
  the exact owner Task/Run/Step versions remain valid, without restoring
  Background dispatch permission. No network or model inference occurs
  under locks.
- `BACKGROUND_RESUMABLE` does not automatically mean an approved action may
  execute in a future epoch. The currently implemented Background grant is
  always **single-epoch**. P4-05 will handle explicit renewed authorization,
  cooperative suspension, and safe resumption.

## Remaining restrictions

The current Tool Gateway has only the read-only `text.statistics.v1` AGENT
step. No external actions, browser research, notifications, arbitrary file
writes or generic model-origin actions are supported by P4-04. Adding any new
capability requires its own explicit permission and effect/idempotency
contract. P4-02b still has a conservative instruction recognizer; unhandled
general permission semantics cannot be claimed as understood by ALIGN-01.

## Acceptance

Real PostgreSQL tests validate separately approved single-epoch permission,
end-to-end GOAL/P2 claim and execution, user P1 not implying Background
permission, untrusted/unstaged operations rejected, duplicate/replayed
approval, stale epoch after return and foreground return during tool
execution. Existing Phase 0–4 tests and live PostgreSQL/Ollama CI must pass;
independent Codex review required before merge.
