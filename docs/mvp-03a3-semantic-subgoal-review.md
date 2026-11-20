# MVP-03A.3 — Semantic Subgoal Review and Goal-Owned Adoption

**Authority:** Persistent Persona Agent Architecture v2.5.1 §§6.1 and 21,
the confirmed ALIGN-01 ownership and source-alignment decision. The
November 20 contract reconciliation amendment remains **proposed**;
this implementation follows its useful revision-level review separation
without claiming the entire package was adopted.

## Flow

```text
Previously approved primary objective
    → bounded general subgoal proposal (MVP-03A.2)
    → PENDING_REVIEW (no child goal and no executable authority)
    → targeted semantic model review (NO database transaction)
    → schema/evidence validation (model remains untrusted)
    → Goal owner recomputes authorization, full active ancestor chain,
      selection generation, parent version, immutable proposal and capacity
    → Goal-owned decision and durable audit:
        ACCEPTED    → emit child goal, revision, event/outbox
        DEFERRED    → unresolved/unsafe new commitment, no child
        REJECTED    → drift or block, no child
        SUPERSEDED  → stale parent/proposal, no child
```

The public entry point is
`goal_semantic_review.review_pending_subgoal(connection, actor, command, gateway)`.
It uses the existing `TextModelGateway.generate` adapter and accepts only
`TrustedPlatformActor(source_id="goal.semantic_review.v1")`. The accepted
goal's title derives from its reviewed **outcome**, not from arbitrary
candidate title text. The original user's primary objective remains unchanged.

The model is instructed to distinguish actual subgoal contribution from
mere plausible wording. It returns a strict, evidence-linked JSON
`SemanticSubgoalFinding`: `ALIGNED`, `UNCERTAIN`,
`DRIFT_DETECTED`, or `BLOCKED`. The host independently checks that both
quoted evidence snippets occur in the persisted fields. Missing/invalid
model evidence fails without changing the candidate.

**Only an `ALIGNED` finding satisfying owner-side conditions** is
eligible for adoption: within the parent's scope, measurable outcome,
supporting rather than replacing the objective, no new permissions,
commitments or material external actions, bounded title, and no duplicate
existing sibling. Reviewer-supplied booleans do *not* grant new
tools, notification permission, or work admission. Side-effect-sensitive
subgoals are conservatively deferred. This is an initial restricted
policy, not a proof of semantic truth or real-world success.

## Persistence and recovery

Migration 0040 adds terminal proposal states and an immutable
`goal_subgoal_semantic_reviews` ledger (original parent event, reviewed
parent version, review method, model ID, evidence quotes, owner decision
and event provenance). Migration 0041 adds
`DELEGATED_REVIEWED` to `goal_records.creation_authority`.
Review calls use P0 command receipts and transactional outbox.

A model failure or malformed output leaves the proposal
`PENDING_REVIEW`; it can be retried. After a completed review, same-key
replay returns the previous result **without model inference**.

**Already-stale proposals are retired before inference begins:** if the
parent is no longer active, its revision differs from the proposal's pinned
version, or its source event changed, the Goal owner locks the parent and
proposal, rechecks staleness, then atomically writes `SUPERSEDED`, a
`goal.subgoal.superseded.v1` event, transactional outbox entry and command
receipt. This avoids wasting a model call and removes the proposal from
`list_pending_subgoal_proposals` and the 24-candidate pending budget.
Pre-review supersession has `alignment=None` because no model assessment
occurred; its status-change event provides the audit trail instead of
fabricating a semantic review record.

Any newer parent revision also invalidates an **in-flight** review result;
that proposal is marked `SUPERSEDED` rather than adopted.

There is no DB transaction or row lock held during LLM evaluation.
Final state mutation uses the existing selection-generation and
full-ancestry locking contracts.

## Limitations deliberately not bypassed

- Targeted review is **invoked explicitly** by the Goal evaluator; this
  increment does not add a scheduler or Background-epoch operation.
  Authorized P2 scheduling is a separate integration, not implied by an
  accepted Goal-domain review.
- Approved child goals do **not** grant execution authority for Tasks,
  Procedures, Notifications or Tools.
- Semantic assessment is uncertain by nature: ambiguous, consequential or
  newly obligating proposals stay deferred for further evidence/approval.
  A single model claim cannot extend the user's delegation. The DEFERRED
  state is currently terminal for that candidate; reopening on new evidence
  is a future explicit Goal-owner revision contract, not an implicit retry.
- The policy is intentionally conservative for privacy-sensitive,
  third-party, financial and other consequential proposed outcomes.

## Verification

`tests/test_phase3_goals.py` includes real PostgreSQL flows for adopted,
rejected, deferred and superseded candidates, invalid model evidence, strict
scope flags, replay without repeat inference, parent status revocation during
model work, parent CAS changes during inference, pre-inference retirement
of stale siblings (without model calls), replay, removal from the pending
budget, outbox/provenance, and further decomposition of reviewed children.
