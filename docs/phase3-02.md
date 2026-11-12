# P3-02 — Actionability and Task Dependencies

**Architecture authority:** Persistent Persona Agent Architecture v2.5 §§6, 9, 14, 17.2, 21–23. This is the second incremental Phase 3 slice. The established hierarchy remains **Goal → Subgoal → Strategy → Procedure → Task → Operation**; this slice links concrete tasks directly to approved goals until procedures exist in P3-03/04.

## Delivered contracts

- `migrations/0015_task_actionability.sql`: authoritative scoped task records, prerequisite DAG, action-bound user-confirmed completion evidence and append-only priority revision history. `task_records` owns its version and immutable goal link.
- `task_contracts.py`: strict typed commands and task/readiness read models.
- `task_state.py`: scoped receipt → Foreground/generation check → authenticated source/approval check → domain validation → event + outbox + versioned state commit. Task reads are selected-persona fenced. No network/LLM call occurs under lock.
- `tests/test_phase3_tasks.py`: real PostgreSQL acceptance for dependency readiness, cycle prevention including RUN_AFTER, optimistic concurrency, user priority, ancestor pause, cross-persona privacy, independent completion evidence and replay.

### Actionability versus execution

`TaskView.actionability` is a **planning/readiness** evaluation, not a scheduler admission. It checks current task status, every ancestor goal status, user suspension, exclusive-ONLY preferences, dependency and RUN_AFTER completion, deadline and missing agent capability. A task can be `READY` while `dispatch_authorized=false` because the P0 operation scheduler intentionally still rejects all `task_id` submissions. P3-04 or a dedicated follow-on integration must create the versioned task gate with actor/permission, cancellations, eligible dependencies and post-tool evidence reconciliation **before** permitting task-owned operations.

The user can explicitly create USER or AGENT work. A USER task requires a concrete action and observable completion criterion; its report becomes `USER_CONFIRMED` only after a new authenticated user action and an action-bound confirmation. This is stronger than advice issued, but it is *not independent verification*. AGENT tasks require `OPERATION_RESULT` evidence and cannot be marked complete in P3-02. Neither task completion nor a claim from a language model marks an approved goal achieved.

### DAG and concurrency

Task dependencies can refer to tasks under different goals only within the same User × Persona instance. All writes are scoped and idempotent. Dependency additions and RUN_AFTER/complete/cancel edits acquire a short **instance-scoped advisory transaction lock** to prevent write-skew in graph validation; unrelated goals and domain writes remain concurrent. The recursive reachability check traverses both prerequisite edges and RUN_AFTER constraints. Target task version CAS plus a bounded max of **32 prerequisites per task** and **64 tasks per goal** limit expansion. A cycle, self-edge, duplicate, cross-persona edge or retroactive dependency on completed work is rejected.

Goal status changes never rewrite task history. A paused/abandoned ancestor makes descendant tasks ineligible at read/confirmation time. Explicit cancellation does not cascade to dependents; they remain blocked until a future replan.

### User priority semantics

Each task supports one bounded preference and one RUN_AFTER target pending richer planning, plus SUSPEND/RESUME, SET/CLEAR_DEADLINE and exclusive ONLY/CLEAR_ONLY. `PREFER_OVER` is **not exclusive**, and does not block the less preferred task. `ONLY` makes other tasks in that instance ineligible for future admission; it is unique per instance. `RUN_AFTER` is a *user ordering constraint*, not an operation edge or evidence of completion. All changes are explicit, versioned and recorded in `task_priority_revisions`. No automatic priority-based worker dispatch is implemented.

### Approval and provenance

For this initial slice, each mutating command needs a distinct authenticated turn matching `task_approval_phrase(command)`, tied to the exact action/source and valid within 24 hours. This is a conservative provisional domain proof consistent with P3-01, not a finished conversational confirmation flow. The caller must show the full proposed action before asking for confirmation. A user quote or inferred "yes" alone does not grant write authority. Later dialogue integration may replace these phrases with equivalently bound, auditable approval receipts.

## Deferred work

P3-03/04: versioned STABLE/PARAMETERIZED/ADAPTIVE procedure definitions, `ProcedureRun` steps, instruction-delivery distinction and task-owned operation admission gates. P3-05: evidence sufficiency, independent agent result verification, feedback-based adaptation, priority/deadline conflict arbitration and authorized derived-task generation. P3-06: integrated long-running guided procedures, resource fairness, restart and contextual goal evaluation.

**Phase 3 is not complete with this PR.** A passing P3-02 gate shows a bounded approved goal can be linked to tasks with independently specified completion predicates, and that dependencies, priorities and false completion are handled without accidental tool execution.
