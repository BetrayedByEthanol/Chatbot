# MVP-03A.4 — Autonomous Goal Evaluation and Decomposition Scheduling

**Architectural authority:** v2.5.1 §§6.1, 8.2–8.3, 10.1, 21 and 22.2.
This is a *bounded Goal-owned P2 opportunity-evaluation slice*, not a new
generic autonomous task executor or a replacement for P4 ALIGN-01.

## Contract

The normal P4 Background preparation barrier remains authoritative.
A separate SQL-only scheduler pass (`run_goal_evaluation_scan`) finds
`background_ready_epochs` whose selection is still BACKGROUND, generation
matches, and preparation status is READY. It stages **at most one eligible
Goal revision per epoch**, using `goal_autonomy_jobs` and a deterministic
operation ID. It skips branches with children or pending proposals and does
not reevaluate the same goal/version on each polling tick.

The staged job is then enqueued through the **existing P0 operation
scheduler**, as `goal.evaluate_decomposition.v1`, with:

- `origin=GOAL`, `priority=P2`, `BACKGROUND_RESUMABLE`.
- `effect_class=NONE`, `task_id=NULL`, capability `goal.evaluation`.
- Existing `cognition.llm` resource pool and per-persona concurrency group.
- Stage-before-admission, exact READY epoch, current parent goal version and
  source-event checks at enqueue and claim. Goal evaluation has its own
  taskless read-only gate: it cannot exploit the separate P4-04 Procedure/Task
  authorization or gain a tool.
- Shared Background P2 admission budget: at most 2 active operations across
  Procedure and Goal evaluations. Existing P0 resources, claim leases,
  preemption and retry/recovery apply.

Worker: `python -m persona_agent.goal_autonomy_worker --dsn ... --model ...`.
No separate broker, scheduler, event store or resource manager is added.
The worker claims only its registered Goal evaluation operation type.

## Inference and ownership

The worker reads one approved goal plus up to three **user-reported** goal
evidence records while holding a fenced P0 lease. It releases the transaction
before calling the existing model gateway. The model may return **one**
structured supporting milestone or `candidate=null` (abstain). A candidate
is a suggestion, never a user instruction or proof of goal completion.

The Goal owner uses `propose_derived_subgoal` from MVP-03A.2. This
entry point additionally accepts an **exact RUNNING Goal P2 lease**
**only for** `TrustedPlatformActor(source_id='goal.autonomous.evaluator')`.
The same Goal transaction verifies P0 claim_generation, worker, lease expiry,
current selection mode/epoch/generation, READY barrier, staged job's
goal/version/event pins, full locked active ancestor chain, provenance,
deduplication, and parent CAS. Old explicit Foreground Goal evaluators are
unchanged. The two sources have distinct authorization gates.

Mechanical proofs may produce `DELEGATED_VERIFIED` goals under the already
approved parent; other candidates become `PENDING_REVIEW` and remain
non-actionable until the independent MVP-03A.3 semantic review. The model
never grants a task, tool, strategy-selection, notification or operation
authorization. The parent's approved outcome never changes.

A null result marks the job `NO_PROPOSAL`. One accepted/staged proposal marks
`PROPOSED`; both finalize their P0 operation. Job results are durable and
stable per goal revision. Proposal IDs and Goal command idempotency keys
are derived from the scheduled operation ID, enabling crash-safe replay if
proposal creation committed before job completion.

## Limits / next integration

- This slice schedules one **Goal evaluation** per READY epoch. It does
  not auto-review every pending candidate or autonomously execute Tasks.
- Background task admission continues to require P4 ALIGN-01 and
  Procedure/Task-specific approval. An accepted child has no tool authority.
- The initial planner only expands useful empty branches; child-goal revisions,
  more nuanced opportunity triggers, periodic long-horizon reassessment and
  multi-strategy discovery remain subsequent increments.
- A model/provider failure exhausts the existing three bounded P0 attempts;
  it does not create unbounded follow-up operations.
- Goal evaluation must not delay user conversation: P2 capacity has no claim
  priority over P0/P1, and returning to Foreground invalidates Background
  commits by selection/epoch checks.

## Verification

`tests/test_mvp03a4_goal_autonomy.py` covers real PostgreSQL approval,
readiness, one-per-epoch idempotent enqueue, P0 claim and completion,
mechanically accepted and review-only outcomes, null model output, and
unstaged/forged P2 admission.
