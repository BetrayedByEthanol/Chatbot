# MVP-03A.1 — Delegated Subgoal Authorization: bounded first slice

Authority: **Persistent Persona Agent Architecture v2.5.1 §§6.1, 6.2, 7.2,
21.1**, including the distinction between a user-approved primary goal and
supporting, agent-derived subgoals. Goal-Driven Capability Opportunity Discovery
(CAP-OPP-01) is still a *proposal*, and the November 20 CRA reconciliation
amendment is also *proposed*, not adopted. Neither automatically grants new
capability, task, or external execution rights.

## Contract

- **Direct goals remain user-approved.** Existing `create_goal()` and its
  exact, separate approval-turn challenge are unchanged. This slice does
  **not** implement natural-language creation of a primary goal.
- **A delegated subgoal does not need a second user approval** if Goal can
  independently demonstrate a bounded contribution to an existing approved
  objective. It must not rewrite the primary goal, raise its target, or grant
  new tools or communication permission.
- **Current deterministic proof is deliberately narrow:** a weekly walking
  goal with agreeing, complete desired-outcome and success-criterion fields.
  An authorized Goal evaluator may derive a strictly smaller walking-frequency
  milestone (e.g. 3 walks/week → 1 walk/week). All other objective forms
  abstain. The child is a **milestone toward the original target**, not a
  replacement or automatic claim that walking occurred.
- **Domain ownership:** `goal_state.derive_weekly_walking_milestone()`
  alone commits the child. It requires a trusted platform actor with
  `source_id='goal.delegation.v1'` and a selected Foreground generation.
  A public user command, an LLM output, or an arbitrary event cannot use
  this pathway as authorization.
- **Bounded hierarchy:** the **entire ancestor chain**, including the
  directly approved root, must be ACTIVE at the time of child creation.
  Parent status alone is insufficient because pausing/abandoning an ancestor
  does not cascade to existing descendants. Ancestry is fetched read-only,
  then all chain rows are locked in stable `goal_id` order; the immediate
  parent is locked `FOR UPDATE` for its version bump and all higher ancestors
  `FOR SHARE` to fence concurrent status changes. The Goal owner validates
  the full locked chain before any event/insert. This matches the ordering
  principle in `task_state._active_goal_chain` and avoids parent-first
  lock inversion. Existing depth/child-count limits, parent CAS, duplicate
  prevention, unchanged parent target, and authenticated ancestor provenance
  remain mandatory. Both parent goal fields must agree on the numeric target.
- **Durability:** ordinary P0 command receipt and idempotency, Goal-owned
  event stream/outbox, atomic insert and parent version bump. The created
  `goal.created.v1` event has `TRUSTED_PLATFORM` trust and an explicit
  delegated-parent source lineage, not `user_approved=True`. The goal
  record's `creation_authority='DELEGATED_MILESTONE'` allows readback
  without misrepresenting it as directly confirmed by the user.
- **No model/network under locks.** Pure numerical contribution is checked
  in one short Goal transaction. Initial execution is restricted to
  selected Foreground; Background needs its established READY epoch,
  authorization and alignment contracts and is not enabled by this API.

## Example

1. The user directly authorizes a primary goal with desired outcome
   **"Walk three times per week"** and success criterion
   **"Complete three walks per week"** through the existing Goal API.
2. Goal evaluation sees an actionable intermediate milestone.
3. Goal calls `derive_weekly_walking_milestone(..., weekly_walks=1,
   parent_goal_id=..., expected_parent_version=...)`.
4. The Goal owner validates the existing authorization and quantified
   contribution, creates an active `SHORT` child **"Walk 1 time per week"**,
   and preserves the primary 3-walk outcome.
5. Replaying the same command returns its original receipt; a competing
   stale-parent version or duplicate child is rejected.

## Deferred, not silently granted

- Generic decomposition of natural-language goals, model-assisted semantic
  contribution review, cross-goal dependencies and automatic decomposition
  scheduling. Uncertain contribution needs an owner-governed *review* path,
  not a model assertion accepted as proof.
- Goal-derived **Task** admission and **Notification** scheduling. Existing
  `task_state.create_task` and `notification_state.schedule_notification`
  authorization contracts still apply. This change does not bypass them.
  In particular, sending a reminder requires an independently valid user
  notification policy and a specified time.
- Conversational root-goal approval, broader goal/strategy adaptation, and
  Background/Inactive lifecycle autonomy.

## Verification

`tests/test_phase3_goals.py` covers a direct approved parent, delegated
milestone creation, exact provenance, parent invariance, event/outbox,
idempotent replay, equivalent-child prevention, CAS version fencing,
noncontributing/ambiguous inputs, forged actor/source, cross-user scope,
paused or abandoned **root** with an otherwise active derived parent, and a
concurrent root-status writer denied a `FOR UPDATE NOWAIT` lock while a
grandchild is being committed.
The existing P3-01, Phase 3 integrated, Phase 4 and full PostgreSQL
contract suites must continue passing.

This is a focused proof that **subgoal creation may be delegated without
repeated affirmation**, not a claim that the full Goal Engine can already
derive arbitrary subgoals, tasks, strategies or reminder times.
