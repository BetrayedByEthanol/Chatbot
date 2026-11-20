# MVP-03A.2 — Generalized Goal Decomposition (bounded integration)

**Base:** v2.5.1 §6.1 and §21.1. Main goals retain authorized outcomes;
derived children must contribute to a parent without silently changing it.
**Confirmed ALIGN-01:** changed derived work must be owner-validated, and
unresolved consequential uncertainty must be gated. The CRA-v2.5.1 amendment
is **proposed**, not automatically adopted; its owner-review split informs
this conservative implementation.

## Implemented contract

`goal_decomposition.propose_derived_subgoal()` is a Goal-owned operation
accepting a strict `ProposeDerivedSubgoalCommand`. Its caller is a trusted
Goal evaluator (`TrustedPlatformActor` with an admitted
`goal.decomposition.v1` source), **not** an LLM tool endpoint.

- The primary objective must already have been authenticated and approved.
  Goal admission locks the *entire active ancestry*, checks expected parent
  version, Foreground selection generation, scope, the authentic creation
  event of each ancestor, parent-child event lineage, depth, breadth and
  duplicates. No new ancestor approval is required for valid delegation.
- A model-proposed candidate **never** acquires authority by its rationale.
  Only a deterministic, exact quantitative proof can admit a child during
  this increment: both parent fields and both proposed child fields must
  describe the same verb, countable unit and day/week/month period, with a
  strictly smaller positive target.
- Example automatically admitted: approved `Read 3 books per month` with
  success `Read at least 3 books per month`; candidate `Read 1 book per
  month` with matching success criterion. The goal owner verifies the
  numerical relationship, records a proposal event and creates a
  `DELEGATED_VERIFIED` child with an immutable source chain.
- Example **not** automatically admitted: `Attend a book club`, or a
  candidate changing the unit/period, or an otherwise unprovable
  parent–child relationship. This is saved as `PENDING_REVIEW` in
  `goal_subgoal_proposals`, with version pins, text, rationale, source
  evidence, an event and outbox entry. It is **not** a `goal_records`
  entry, task, procedure or operation.
- Both outcomes are an idempotent Goal command. Pending proposals do not
  increase parent goal version; accepted children increment that version.
  No external I/O or LLM call takes place inside the Goal transaction.
  Existing exact user approval for new main goals is unchanged.

## Important boundaries and next integration

This slice enables a *general typed proposal interface*, mechanical
acceptance for a wider class of goals, and durable review staging.

It does **not** claim that arbitrary natural-language subgoals can safely
be auto-admitted. A follow-up Goal-owned review/adoption contract must
consume externally obtained review findings, validate their evidence,
versions, authority and contribution, and accept/reject/supersede staged
proposals; no new goal becomes actionable in the meantime.

Likewise, Task/Operation admissions, Background epoch alignment checks,
reminder permissions, and user-directed primary goal changes are separate
owner policies. Do not use the proposal record to bypass them. Existing
`alignment_owner_checks.validate_task_alignment` conservatively labels
derived goal chains for review, which remains a separate workstream before
derived tasks become Background-executable.

## Verification

The P3 PostgreSQL integration suite includes:
1. Approved root → mechanically proven reading milestone without
   an additional user approval.
2. Pending proposal is durable but not an active goal or tool right.
3. Parent source-event forgery, unauthorized actor and idempotency mismatch.
4. Stale parent-version protection, safe nested decomposition and
   paused-ancestor gating.
5. Mismatched unit/period/criterion and action-text contamination
   remain in review rather than becoming approved children.

No new global scheduler, mandatory classifier or model call was introduced.
