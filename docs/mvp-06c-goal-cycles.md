# MVP-06C — Bounded decomposition and recurring goal evidence

Architecture v2.5.1 §§6.1–6.2, 21.1–21.2, 22.1–22.2; confirmed PPA-US-046/047/048/051/057/058/099. Baseline `b412c7584ff928c93ba6d56bb6ac7fe15aeeee50` after PR #128: 2,226 PostgreSQL tests passed, 3 skipped; live integration passed after an observational Ollama timeout rerun. This is a bounded vertical slice building on the existing Goal Engine, semantic contribution reviewer, Task DAG and P0 scheduler; not a new planner, evaluation daemon or executor.

## Gap and implementation

MVP-03A.1–4 already implement bounded recursive goal depth (5) and breadth (12), provenance and version-fenced mechanical/semantic proposal review, one P2 evaluation per ready epoch with existing resource/lease/retry budgets, and Task prerequisites, priority, separate executor and corrected outcome evidence. 06A approves recurring goals in a different exact frequency grammar. 06C unifies the exact paired weekly walk/session forms with the existing count-measure proof, without equating sessions to distance/duration/book counts or mismatched criteria. Legacy walking milestone deduplication uses the same canonical measure. Non-mechanical proposals still require the existing semantic review; its final owner decision retains safety, stale and contribution gates.

Delegated expansion (specialized walk, general proposal and semantic adoption) stops at existing noncancelled tasks. The Background scan skips these branches before staging model work. Existing active child or pending review also prevents the new direct leaf-task planner from opening a competing plan. Task creation bumps the parent version, fencing already-staged evaluations. This conservative policy does not stop independent approved goals, cancel work, or prohibit a separate explicit user-approved child. General multi-branch strategy and alignment review are not redesigned.

## One occurrence at a time

`read_recurring_goal(connection, actor, instance_id, goal_id, window_start=None)` returns a typed, scoped, read-only `RecurringGoalView`. It reuses Goal provenance and Task graph/effective confirmation reads. A supported count is an exact matched positive action pair (e.g. walk twice per week, exercise three sessions per week, prepare two meals per week, read two books per month) of at most 12 occurrences per cycle. Unsupported, upper-bound/negative, vague, mismatched and ambiguous measures abstain rather than invent a metric or change the approved objective.

UTC day, Monday-based week or calendar month uses explicit half-open date bounds. The user must approve the actual displayed cycle and occurrence. No prorating or extension of explicit goal evaluation start/end or target dates; a wholly pre-approval cycle is outside scope. Only the current applicable cycle offers new tasks; previous/future cycles are read-only, with current (not historical as-of) evidence. Calendar choice is an explicit initial implementation constraint, not an inferred user-local schedule.

For an approved two-walk weekly goal:

1. Read the goal cycle: 0 reported, 0 confirmed, draft "Complete one walk (1/2)" with exact action, criterion, USER executor, evidence requirement and UTC window.
2. Submit `CreateRecurringTaskCommand` (extends existing `CreateTaskCommand`) including the draft fields, new task ID and authenticated source. Present the exact task approval phrase; obtain a separate user approval turn; use existing `create_task`.
3. Generated draft, approved task, and user activity are different. No count for advice or a planned task. A separate exact approved DONE report adds one qualified user report; explicit existing task confirmation makes it user-confirmed if prerequisites remain effective.
4. Only then can another draft be offered. Completing two current qualified confirmations meets the count by user testimony, never independently verified achievement. The root remains ACTIVE. The next calendar cycle is independent. A correction invalidates credit and stops another draft until the owner evidence is repaired.

The task owner checks the selected Foreground generation and authentic source, exact action approval, graph/ancestry locks, goal version and creation lineage, current cycle, immutable owner-derived payload, prior effective evidence, competing children/reviews/tasks, exclusive priority and existing per-goal task count budget. Stale approval after task or prerequisite correction is rejected even if the Goal version did not change. At most one unresolved occurrence is admitted; planned, cancelled, suspended, uncertain or dependency-blocked work is not bypassed. Existing task owner controls dependency and priority updates. The normal Task read adds RECURRING_WINDOW_CLOSED to historical/future occurrence tasks; historical reports do not become authorized operations. No new AGENT capability, automatic task report, task completion, parent completion or strategy selection.

## Persistence and evidence

Migration 0054 adds immutable `goal_recurring_task_slots` with scoped task/goal, date bounds, ordinal 1–12, unique goal/start/ordinal and existing task creation event. Slot and task commit in one existing owner transaction with the command receipt and outbox. The draft fields are checked against the exact current owner-generated proposal; only an explicit approved USER task can get a slot. No new task state or stored aggregate counter.

The Goal read projection counts one qualified current report per slot, not generic task/child totals. Reported includes user-confirmed; confirmed is the effective subset. The latest DONE report must reference a distinct authenticated source turn within the cycle and at or after task creation. This establishes timing of the report, not independent occurrence time. Reused evidence across slots is excluded as ambiguous; precreation/out-of-window sources, corrected/needs-review tasks, ineffective prerequisite confirmations and stale report pins do not count. A confirmation may be later than the report window; it attests to the exact earlier report. Raw report/confirmation/correction history stays in the Task owner. No current slot is independently verified in this USER-only increment. Do not equate reports with verified progress, and do not infer attainment of a broader parent goal from a completed child.

## Regression scope

`tests/test_mvp06c_goal_cycles.py` covers 06A goal activation, canonical measures and calendar boundaries, exact task approval, one-at-a-time planning, task reports and separate confirmations, direct and transitive correction invalidation, stale task approval, precreation report exclusion, concurrent slot admission, priority and user scope, unsupported diet upper-bound goal abstention, positive meal and exercise goal planning and supporting child admission, explicit window bounds, and no operation/goal status mutation. `tests/test_mvp03a4_goal_autonomy.py` adds READY Background stop-at-task evidence. Existing goal/delegation/semantic review/Task/Phase 3 acceptance suites remain mandatory.

```sh
python -m pytest -q tests/test_mvp06c_goal_cycles.py \
  tests/test_phase3_goals.py tests/test_phase3_tasks.py \
  tests/test_mvp03a4_goal_autonomy.py tests/test_phase3_integrated_acceptance.py
```

Candidate static review only; PostgreSQL and live integration CI NOT RUN at authoring. No real-model behavioral or controlled benchmark acceptance claimed.

## Boundaries and next increment

The positive-count leaf planner is a typed owner path, not a new conversational approval grammar or automatic action scheduler. General free-form task planning and user-facing goal-progress dialogue, independently verified outcome admission, absolute goal measures, negative/upper-bound diet goals, local timezones, cross-child contribution reconciliation, partially applicable windows, broader priority arbitration and diagnosis remain open. Existing 64 lifetime tasks per goal is preserved: future recurring cycles can be read/planned until the existing budget is reached; no hidden rollover, archival or deletion. A cancelled slot requires explicit review rather than silently replacing it. US-047/057/058 remain P; no story promotion, Phase 2/3 exit or deployment waiver. Focused 06D — authorized strategy research/adaptation follows technical regression.
