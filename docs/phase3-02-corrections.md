# P3-02 follow-up — Task completion corrections and effective dependency evidence

**Authority:** Architecture v2.5.1 and merged P3-01/P3-02/P3-03. This is a targeted prerequisite for P3-04; it **does not** introduce procedure runs, new scheduler lanes, agent tool execution or goal-achievement evaluation.

## Existing contract preserved

- Goal & Task domain owns task states, confirmation truth and dependency readiness.
- A user DONE report is `USER_REPORTED`, `UNREVIEWED` evidence; it never means an operation succeeded, an AGENT capability was executed or a goal was achieved.
- A separate action-bound user confirmation changes a USER task to `USER_CONFIRMED` only under current selected Foreground authority, task-version CAS, active goal chain, effective prerequisites and an exact authenticated confirmation. This is **not** independent verification.
- Corrections must not erase or silently rewrite prior reports, confirmations, dependencies, tasks or approved goals.

## Implemented changes

1. **Latest report binding.** `confirm_user_task` accepts only the most recent applicable DONE report for the task, ordered by the durable Goal/Task domain event sequence instead of wall-clock timestamps. A subsequent DONE/OBSTACLE report prevents confirming an earlier DONE record. The task row and persona graph transaction lock serialize the check with new reports/corrections.
2. **Audited correction command.** `CorrectTaskOutcomeCommand` identifies exactly one current report or latest effective confirmation, cites an authenticated user turn, supplies an explicit reason, binds a separate user approval, and requires expected task version. `correct_task_outcome` atomically appends `goal.task.outcome.corrected.v1`, a correction record, receipt and transactional outbox, and changes **only** the affected task to `NEEDS_REVIEW`.
3. **Reconfirmation.** `task_user_confirmations` retains all past confirmations. Removing the old `UNIQUE(task_id)` constraint allows a new DONE report and a later separate confirmation after correction; each individual report can still be confirmed at most once. Original evidence remains immutable.
4. **Pinned dependency evidence.** On each USER task confirmation, `task_confirmation_prerequisites` snapshots the precise confirmation IDs of every immediate ordinary or `RUN_AFTER` prerequisite. A correction invalidates effective completion of every descendant transitively. Even if an upstream task is later reconfirmed, the dependent task's old confirmation stays invalid until the user explicitly re-reports and reconfirms it. The descendant's historical `USER_CONFIRMED` testimony is **not automatically overwritten**.
5. **Derived projection.** `list_tasks` exposes historical `status` separately from `effective_completion`, `needs_reassessment`, `unmet_dependencies` and the `CONFIRMATION_REQUIRES_REASSESSMENT` blocker. Both reads and confirmation admission calculate the same transitive graph, and read transactions take a shared persona-graph advisory lock to prevent a torn multi-query snapshot.

The migration `0017_task_outcome_corrections.sql` adds the review state, scoped append-only correction/provenance records, historical reconfirmation and pinned dependency references. No existing confirmation or report row is deleted. **Existing pre-migration confirmed tasks with prerequisites but no historical snapshots are conservatively flagged for reassessment** rather than silently trusted; independent tasks remain effective.

## Evidence admission versus execution admission

- A correction is accepted even when a related goal has been paused or the task suspended: correcting reality is not the same as authorizing new work.
- A report can be appended while `NEEDS_REVIEW`: DONE produces `USER_REPORTED_DONE`, OBSTACLE produces `PLANNED`. Confirming DONE still requires all normal goal, task and prerequisite gates.
- The existing P0 `EnqueueOperation.task_id` prohibition remains in force. No correction operation pretends to reverse an already performed external effect; P3-04 must independently cancel/revalidate future tool admission.
- Any method or procedure step state must remain separately domain-owned. Neither a successful procedure nor an instruction delivered to the user automatically completes a task.

## Concurrency and failure contract

- Reports, corrections, confirmations, priorities and dependency changes are serialized by the existing short User × Persona graph transaction lock, in addition to task-row version checks. Readiness reads take the corresponding shared lock.
- Every consequential change requires an exact bound authenticated user confirmation; idempotent retries replay the same committed event, while payload collisions or stale versions fail.
- Direct SQL UPDATE/DELETE of correction, report, confirmation or snapshot history is rejected by database triggers. Transaction failure leaves no partial event, receipt, status or outbox mutation.
- Cross-user/persona report/confirmation IDs cannot be corrected; composite foreign keys preserve scope even if application validation is bypassed.

## P3-04 integration rule

Future `ProcedureRun` evidence can be proposed to the Goal & Task domain, but it must not write `goal_tasks.status` itself. Task completion admission should consume the `effective_completion` and pinned-confirmation contract here, rather than checking raw `USER_CONFIRMED` status. `NEEDS_REVIEW` means evidence requires reassessment; it is not a procedure step failure nor permission to retry tools.

## Acceptance

`tests/test_phase3_tasks.py` adds: latest-report rejection; obstacle and stale-correction rejection; evidence immutability and reconfirmation; three-level transitive invalidation that persists after upstream reconfirmation; and cross-user authority/version fencing. Full PostgreSQL and live-model CI must pass on the new PR head before merge.
