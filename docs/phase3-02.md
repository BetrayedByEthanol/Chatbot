# P3-02 — Actionability & Task Dependencies

Authority: Persistent Persona Agent Architecture v2.5 §§6, 9, 14, 17, 21 and P3-02 from `docs/phase3-01.md`.

## Boundary

This is a **contract-first vertical slice** of the existing Goal & Task domain, not a new scheduler.
`Goal → Subgoal → Strategy → Procedure → Task → Operation` remains distinct.

- `goal_tasks`: concrete, evidence-defined USER or AGENT work associated with an approved goal. `completion_criterion` and `evidence_requirement` are separate; AGENT tasks declare a capability.
- `task_dependencies`: goal-local prerequisite DAG. Edges are created only under current selected Foreground authority, exact user confirmation, parent goal version CAS, and goal-row serialization. A recursive reachability check rejects cycles. Depth/fan-in are bounded by 64 tasks per goal and 16 prerequisites per task (provisional limits).
- `task_outcome_reports`: user report evidence is `USER_REPORTED` and `UNREVIEWED`; `DONE` yields `USER_REPORTED_DONE`, **never** a verified completion. Neither advice nor reported completion satisfies prerequisite dependencies. Verification and evaluation arrive in P3-05/P3-06.
- `task_priority_rules`: append-only user-directed `PREFER_OVER`, `SUSPEND`, `SET_DEADLINE`, and `RUN_AFTER` statements. These are preference records, not executable scheduler admission overrides. A preference to prioritize B is not an exclusive ban on A; deadlines do not grant permission.
- `list_tasks`: derived, per-persona actionability from goal/ancestor activation, task status, prerequisites and agent capability/permission checks. Agent execution remains blocked until Phase 4 connects capability/authorization gates. A user task with all prerequisites clear is *actionable*; it is not **done**.

## Authority and consistency

- Every mutation uses P0 `command_receipts`, an event and atomic outbox in one short transaction. Repeated commands replay across persona switching; new commands are selected-Foreground generation fenced.
- Action-bound confirmation requires a **separate authenticated user turn** matching `task_approval_phrase(command)`, scoped to the same conversation and no later than 24 hours from source. User quote alone is not authority. The Conversation Manager is responsible for presenting the complete proposal before confirmation; a natural-language UX can later replace this provisional explicit phrase using an equally strong bound receipt.
- Goal tree/dependency additions serialize at the **one affected goal row**, with expected goal version, rather than a global lock. Unrelated goals remain concurrent. Task mutations use task entity versions.
- Reads are selected-persona fenced; no cross-persona private task retrieval.
- No LLM, tool/network call or model-based verification occurs in a database transaction.

## Deliberate deferrals

P3-02 doesn't initiate autonomous tasks, issue tools, guarantee agent capabilities, synthesize procedures, or mark results VERIFIED. `RUN_AFTER` and `PREFER_OVER` are recorded constraints for a later priority-aware scheduler, not claims that an executor already enforces them. No automatic recurring-goal decomposition or model-inferred user approval is introduced. The current read model exposes preferred-over and deadline policy; other constraints are recorded durably and will require a richer scheduler-facing view before dispatch.

## Acceptance

`tests/test_phase3_tasks.py` verifies scoped authority, distinct confirmations, idempotent replay, concurrent parent version CAS, dependency reachability/cycle prevention, cross-goal isolation, evidence uncertainty, derived readiness, stored preference and persona-switch fencing. The full PostgreSQL and live-model integration workflows must pass before merge.
