# P4-05b — Validated safe-stop checkpoints and input restoration

Authority: v2.5.1 §§3, 8 and 10; `docs/phase4-plan.md`, P4-05a.

## Scope and ownership

* P0 still exclusively owns operation leases, resource release, cancellation,
  suspension and `operations.checkpoint_ref`. No new scheduler or resume API.
* For the **one registered** Background/P2, READ_ONLY
  `procedure.agent.text.statistics.v1` operation, P0's fenced
  `SUSPEND_REQUESTED -> SUSPENDED` acknowledgement atomically calls the
  Procedure-owned snapshot builder and writes an immutable v1
  `procedure_agent_checkpoints` record. Both snapshot and P0 ref are rolled
  back if its worker claim expires while waiting for resources.
* The snapshot contains only owner IDs/version pins, a SHA-256 checksum of the
  authoritative Procedure instruction, max_words, source claim, background
  epoch and the fixed `READ_ONLY_RESTART_REQUIRED` boundary. It deliberately
  does not copy an LLM/tool output, any credentials or free-form user input.
* If the owner stage/step was already invalidated, the stopped worker can
  still be suspended safely **without a restorable checkpoint**. A crashed
  worker reclaimed by P0's lease reaper gets no fabricated checkpoint.
* The authenticated user's `load_validated_procedure_checkpoint` returns a
  typed **data-only** restoration candidate. It requires the original persona
  still selected in Foreground with its selection generation intact, source
  operation SUSPENDED/PREPARED, a matching checkpoint pointer, current
  authorized Procedure stage, Goal/Task/Run/Step validity, a live Task deadline
  and a matching instruction digest. Invalid or stale input fails closed.
* Loading never enqueues, claims, runs a tool, marks Task done, updates a
  Procedure step, or extends an epoch. A tool that had started before
  Foreground return may only start **from the beginning**, if later explicitly
  reauthorized. No mid-call state is assumed recoverable.
* The P4-04 single-epoch grant remains expired. P4-05c must define a distinct
  renewed user authorization and owner-validated re-admission contract before
  any execution can proceed.

## Failure boundaries

Checkpoint creation + P0 suspension + resource release + event/outbox occur in
**one short transaction**. A cancelled worker cannot write a checkpoint; an
expired worker cannot commit one; an uncertain external effect never qualifies.
No tool/LLM call occurs under SQL locks. Existing generic P0 checkpoints and
`resume_suspended` are not promoted to this specialized P2 restoration API.
