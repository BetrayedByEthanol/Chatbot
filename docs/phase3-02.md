# P3-02 — Actionability & Task Dependencies

**Authority:** Persistent Persona Agent Architecture v2.5 §§6, 9, 14, 17, 21 and the P3-02 increment in `docs/phase3-01.md`.

The established distinction remains **Goal → Subgoal → Strategy → Procedure → Task → Operation**. This is a bounded Goal/Task-domain increment, not an operation dispatcher or new scheduler. P3-01's user-approved goal outcome stays unchanged by strategy, task or priority updates.

## Contract and persistence

- `goal_tasks`: user/persona-scoped, versioned USER or AGENT work. Concrete `action_description`, `completion_criterion`, and `evidence_requirement` are distinct. Evidence policy is `USER_CONFIRMATION` for USER and `OPERATION_RESULT` for AGENT. Agent work requires a named capability but remains non-dispatchable.
- `task_dependencies`: prerequisite DAG across tasks of the **same User × Persona**, including different goals. Foreign keys and domain guards reject cross-persona edges, cycles, self-links, duplicate edges, cancelled prerequisites and stale goal versions. Initial bounds: 64 tasks per goal, 16 prerequisites per task.
- `task_outcome_reports`: a user's DONE/OBSTACLE statement is `USER_REPORTED` with `classification_status=UNREVIEWED`. DONE puts the task in `USER_REPORTED_DONE`, **not completed or verified**. It does not unblock successors.
- `task_user_confirmations`: a *separate*, action-bound authenticated confirmation of a specific DONE report moves USER work to `USER_CONFIRMED`. This is accepted user evidence of doing an action, **not independent verification, tool success or goal achievement**. A user-confirmed predecessor may unblock follow-on USER work. AGENT work cannot use this path; P3-05/operation integration must validate its results.
- `cancel_task`: versioned, user-approved cancellation clears any exclusive priority but does not cancel operations (none can be task-owned yet) or silently cancel dependents. A confirmed task cannot be cancelled retroactively.
- `task_priority_rules`: append-only audit history plus current task-owned priority projection. Supports `PREFER_OVER`/`CLEAR_PREFERENCE`, `SUSPEND`/`RESUME`, `SET_DEADLINE`/`CLEAR_DEADLINE`, `RUN_AFTER`/`CLEAR_RUN_AFTER`, and `ONLY`/`CLEAR_ONLY`. `PREFER_OVER` is never exclusive. `ONLY` is explicitly exclusive for the persona and clears when its task is cancelled or user-confirmed. A deadline does not grant execution authority.
- `list_tasks`: derives planning readiness from goal and ancestor status, prerequisite plus RUN_AFTER completion, current exclusive/suspension/deadline policies, and capability/permission checks. It exposes blockers and `dispatch_authorized=false`. Agent tasks stay blocked until a real permission/capability admission gate exists. Overdue user work can still be truthfully confirmed late, retaining deadline provenance.

## Authorization, locking and recovery

- All writes use P0 command receipts with scoped idempotent replay, Foreground generation fencing, action-bound confirmation from a **separate authenticated conversation turn**, domain events, and the transactional outbox. A source quote alone is not approval; the Conversation Manager must show the concrete proposed change before collecting an explicit confirmation.
- Task creation takes the target goal row's optimistic version/lock to bound fan-out. DAG mutations and task priority/confirmation/cancellation share **one User × Persona advisory transaction lock** to prevent cross-goal write-skew. Unrelated users/personas and goal-domain work remain independent; no process-wide scheduler lock is introduced.
- The full goal **ancestor chain is SHARE-locked** while admitting a user confirmation. This fences P3-01's goal status transitions, which acquire UPDATE locks, addressing the concurrent-pause race raised by the Codex review on PR #19. Task/goal row checks and journal/outbox writes remain inside a short PostgreSQL transaction; no model/tool/network call runs under lock.
- DAG validation traverses both ordinary dependency edges and active RUN_AFTER edges. All edge and priority modifications are versioned and user approved. Read views use selected-persona fencing, with no cross-persona private task import.

## Comparison of PR #19 and PR #20

PR #20 remains the implementation branch; worthwhile capabilities from PR #19 were adapted to its existing contracts:

| PR #19 capability | PR #20 disposition |
|---|---|
| Task action description and explicit evidence policy | **Adopted** while keeping a separate evidence requirement |
| Prerequisite edges spanning goals within one persona | **Adopted**; cross-persona edges remain rejected |
| Mixed dependency / RUN_AFTER cycle detection and enforced readiness | **Adopted** with scoped graph serialization |
| Explicit ONLY/CLEAR_ONLY, clearable preferences and reversible ordering/deadlines | **Adopted**; no implicit exclusivity from PREFER_OVER |
| USER_CONFIRMED completion from authenticated evidence | **Adapted** as a second step after an UNREVIEWED DONE report; no independent VERIFIED status |
| PR #19 Codex finding: ancestor pause race during completion | **Addressed** by goal-chain SHARE locking through user-confirmation commit |
| Immediate task-owned operations, autonomous scheduling, general goal achievement | **Deferred** under v2.5 owner/authority boundaries |

Do not merge both implementation branches. Their SQL migration 0015 files and task module names conflict. Merge/review only one task implementation after CI and human review.

## Deferred and acceptance gates

- P3-03/04 own STABLE/PARAMETERIZED/ADAPTIVE procedure templates and durable procedure runs; subsequent task-operation admission needs a **versioned task gate** in the existing P0 scheduler, not a new scheduler.
- P3-05 will evaluate whether user-confirmed outcomes actually satisfy task/goal success predicates and independently verify AGENT results. No subjective report, generated advice or model interpretation is a tool success.
- P3-06 integrates long-running dialogue, cancellation/restart, resource fairness and safe adaptation. Natural-language confirmation UX remains provisional; exact bound confirmation phrases are a domain safety contract, not the final experience.

**Tests:** `tests/test_phase3_tasks.py` exercises confirmation vs report, mixed-edge cycles, cross-goal/cross-persona scope, exclusive vs preferred priority, reversible priority, idempotency, concurrency, cancellation, stale generations and ancestor-pause fencing. Full PostgreSQL and real Ollama CI must pass before review/merge.
