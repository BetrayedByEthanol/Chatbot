# P4-02b — Source-linked critical domain update integration

Architecture authority: Persistent Persona Agent Architecture v2.5.1 §§7–10, 20.4, 22.4; P4-02a is the parent slice. This increment **does not** grant P2 readiness, execute arbitrary user instructions, or apply Cognitive Engine proposals to authoritative state.

## Contract

At `CRITICAL_PENDING`, the Background coordinator uses the selected-persona shared lock and per-epoch exclusive row lock. The existing immutable conversation snapshot bounds all source scanning; the separate per-conversation `critical_committed_watermark` prevents reprocessing an already accepted historical range after a later Background departure. A bounded 24-turn scan increment persists only candidate type and exact authenticated source turn ID—not a copy of personal text. Bounded verification reads Memory, Goal and Task *owner* persistence. All coordinator writes happen in the same short transaction as a stage event and outbox record.

- **Memory:** Only the existing positive-allowlist `parse_explicit_claim` extracts a candidate. The Memory domain's `memory_extraction_candidates` decision must exist; `ADMITTED`, `CORROBORATED`, `STALE` and authenticated `APPROVED` are resolved. `CONFLICT`, missing extraction and pending candidates remain unresolved; model interpretations cannot approve them.
- **Goal:** Clear direct goal-change requests are flagged for review. A matching `goal_records`, `goal_status_revisions` or `goal_strategy_selections` owner row linked to an authenticated source turn and its owner event is evidence of a committed Goal decision. A model proposal or an unrelated event does not count.
- **Task:** Clear direct priority/cancel requests are flagged. `task_priority_rules` or a source-linked, authenticated cancelled-task command receipt plus confirmed Task projection are accepted; otherwise the source remains pending.
- **Instruction:** Explicit tool/background/notification restrictions are flagged but **cannot** be silently marked handled. There is not yet a general purpose, versioned authoritative instruction/permission domain. Such candidates remain `PENDING` and block this epoch's transition. This intentional fail-closed limitation must be resolved by a later contract before this type of instruction can be enacted/cleared.

The recognizers are **conservative sentinels, not full pragmatic intent extraction**. The Cognitive Engine still lacks typed actionable critical-change proposals, so any conversation without a flagged phrase cannot be assumed semantically complete for autonomy. Consequently `ALIGNMENT_PENDING` is a **handoff for P4-03** rather than authorization to execute; the broader change-focused ALIGN-01 check and owner/domain completion contracts remain required. No new Goal/Task command is auto-created.

## Durable state

- Migration `0023_background_critical_updates.sql`: `background_critical_updates` source/epoch/domain ledger with `PENDING` or `VERIFIED`, optional domain-owned proof reference; `critical_scan_sequence` per frozen conversation; `critical_committed_watermark` in Conversation. Extends the epoch stage set with `ALIGNMENT_PENDING`. All scoped constraints and checks retain privacy and generation boundaries.
- `background_critical_updates.process_critical_updates`: bounded source classification, identity-preserving owner proof reconciliation, and selective watermark advancement on completion. `list_critical_updates` is a read-only authenticated review surface.
- `background_coordinator.advance_background_preparation`: stages progress from `SUMMARIZE → EVALUATE → CRITICAL_PENDING → ALIGNMENT_PENDING`, with a persistent `BLOCKED` state on damaged frozen source. In-progress `CRITICAL_PENDING` remains polled fairly.
- `task_state.cancel_task` now includes authenticated source-turn provenance. Pre-existing events lack this field, so migration `0024_task_cancellation_provenance.sql` and Task-owned `ConfirmCancellationProvenanceCommand` provide an audited repair: the user supplies a fresh attestation and separate exact approval, binding the old cancellation event to its historical turn. The Task owner emits a new authenticated proof event and append-only record without changing the cancelled task or retrying its operation.

## Safety and failure behavior

The coordinator does not infer or persist a goal/priority from natural language. It only recognizes potential review needs and verifies already committed domain-owned effects. A pending change does not vanish on restart, correction or persona switch; an old epoch is superseded and a new epoch starts at the last fully committed critical watermark. No row is auto-cleared merely because the user left or the model said a task was done.

P4-02b has no API to forge a `VERIFIED` row. The actual owner must commit via its existing user-authorized command and evidence contracts. For important instructions without an owner integration, return to Foreground for an explicit correction/implementation instead of starting P2 work.

**Completion boundary:** P4-02b implements the first safe reconciliation slice of the critical update phase, not generalized NLP instruction execution. P4-03 must validate changed derived-work and consequential persona/evidence alignment, commit critical permission/instruction restrictions through a responsible owner, and insert `background_ready_epochs` only once every required gate is satisfied. The selected Background instance remains not-ready here.

## Acceptance tests

P0-P3 regression suite plus PostgreSQL tests for successfully completed no-critical review, Memory extraction conflicts without silent overwrite, approved Goal status mutation proof, unresolved action restrictions, immutable source bound, no premature P2 readiness, return/switch fencing, and crash/replay scan progress. Run live Ollama/PostgreSQL acceptance and independent Codex review before merge. The stacked branch must not be merged without explicit user approval.
