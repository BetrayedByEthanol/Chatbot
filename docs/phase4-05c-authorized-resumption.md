# P4-05c — Explicitly authorized next-epoch resumption

Architecture authority: v2.5.1 and `docs/phase4-plan.md`. No new
scheduler, generic tool runner, or autonomous permission provider.

## Command

`renew_background_agent_step(actor, RenewBackgroundAgentStepCommand)` is
authenticated and uses the existing separate literal approval turn, immutable
source turn, foreground selection generation and exact NEXT Background epoch.
Additional required pins: `resume_from_operation_id` and
`resume_checkpoint_ref`. Command + receipt + Procedure event/outbox + new
stage + immutable source lineage are committed atomically.

The command allocates a **NEW** operation ID for the registered read-only
`text.statistics.v1` step; the old P0 SUSPENDED operation is not revived,
and the old authorization is atomically superseded. No old lease, worker,
background epoch, resource reservation, attempt/result, or approval is reused.
The new approval must match all original Task/Run/Step versions, fixed
capability, parameters and instruction checksum. If any owner input is
stale, the renewal fails closed without changing the source stage.

After the *next* Background transition, the existing P4 preparation barrier
must advance to READY, including updated critical instructions, task
alignment and priorities. Only then can the P4-04 producer admit the NEW
grant. The existing P0 worker independently checks the new grant and the
immutable predecessor checkpoint at enqueue, claim, before tool dispatch
and completion; interruption restarts this synchronous read-only operation
from the beginning, not an imaginary mid-call position.

## Failure and lifecycle behavior

* A stored checkpoint is **data**, never permission. Reading a checkpoint or
  starting Background cannot execute the tool; user must issue new approval.
* Without a valid immutable safe-stop checkpoint, no renewal is possible.
  Expired reaper claims and uncertain/external effects are excluded.
* Cancellation of the source before executing the renewed operation blocks
  the renewed gate. A changed owner input, task deadline, or mismatched
  provenance likewise fails closed.
* Only one renewal may consume a particular source checkpoint; immutable
  lineage records historical attribution and restricts duplicate branches.
  New user authorization starts a new bounded P0 attempt budget, not an
  invisible extension of a suspended claim.
* Failures of _command roll back source supersession, new stage, lineage,
  journal, receipt and outbox. P0 and Procedure owners retain their state
  boundaries. P4-05d will address additional bounded recovery policies.
