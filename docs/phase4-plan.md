# Phase 4 implementation plan — Architecture v2.5.1

Status: proposed milestones, implementation started on P4-01a. Authority:
`Persistent Persona Agent Architecture v2.5.1`, §§3, 7–10, 14, 23–24;
`Persona Architecture v2.5.1 Behavioral Implementation Refinements` (ALIGN-01).
This plan does not redefine the existing P0 scheduler or domain ownership.

Baseline inspected: `main` at `efa86e893d39ed31f0a779b99e3098c4d7ee6469`
(PR #26). Phases 0–3 are merged; the historical legacy migration audit is
not evidence about this current tree.

## Current reuse inventory

| Area | Concrete implementation | Phase 4 gap |
| --- | --- | --- |
| Persona authority | `lifecycle.py`, `persona_selection`, event/outbox, selection generation, presence revision, background epoch | Lifecycle entry currently produces no durable preparation work; an idle callback/host integration needs validation. |
| P0 execution | `operation_scheduler.py`, `operation_control.py`, `operation_contracts.py` | P2 claim already requires `background_ready_epochs`, but no producer may legitimately mark it READY yet; agent-origin P2 admission is not implemented. |
| Conversation | `conversation.py`, `dialogue_state.py`, `conversations`, `conversation_turns` | Need a Background input boundary and a critical instruction/correction handover. |
| Summary | `summary_memory.py`, `automatic_summaries.py`, `conversation_summary_jobs`, `summary_watermark` | Normal range admission retains recent turns and is not proof that all required Background input is summarized. |
| Cognition | `cognitive_engine.py`, `automatic_cognition.py`, `cognitive_watermark` | Reflections are non-authoritative proposals; critical goal/instruction commits need owner acceptance. |
| Memory | `memory.py`, `memory_extraction.py`, `memory_retrieval.py` | Must resolve action-critical corrections/conflicts to the owning domain or block affected work. |
| Goals/tasks | `goal_state.py`, `task_state.py`, dependency/priority gates | Need domain-owned selection of eligible autonomous work after barrier. |
| Procedures/tools | `procedure_agent_gate.py`, `procedure_agent_state.py`, `tool_gateway.py` | Existing authorized AGENT path is user-origin P1 and only permits read-only `text.statistics.v1`; never interpret this as background authority. |

## Implementation milestones

### P4-01 — Background epoch admission and lifecycle integration
- **Owner:** Persona Runtime, with a narrow Background coordinator record.
- **Reuse:** `enter_background`, `record_primary_activity`, `switch_persona`, selection-row lock, generation/epoch fencing, event/outbox, current P2 gate.
- **Contracts / schema:** Create one durable pending preparation record for each successfully entered epoch. Transactionally supersede it upon foreground return or persona switch. Later integrate the authenticated activity and departure sources without treating tool callbacks as presence.
- **Invariant / failures:** Idempotent lifecycle replay yields no duplicate epoch. Failed epoch creation rolls back the mode transition and event/outbox. Mere PENDING entry never grants `background_ready_epochs`.
- **Dependencies:** P0-02/04. **Type:** production code + PostgreSQL tests.
- **Acceptance:** lifecycle record/event atomicity, replay, cross-instance isolation, stale callback rejection, return/switch supersession, P2 remains unclaimable before readiness.
- **First slice P4-01a in this branch:** only the transactional epoch record and tests. Does not start worker cognition, autonomously schedule or grant READY.

### P4-02 — Durable strict conversation preparation barrier
- **Owner:** Background coordinator sequences existing Conversation, Memory and Cognitive owners.
- **Reuse:** P0 operations/event delivery, `summary_memory`, `automatic_summaries`, `cognitive_engine`, input/summarization/cognition watermarks.
- **Contracts / schema:** Versioned stage progress and immutable input boundaries per applicable conversation; stage-specific idempotency/retry and evidence of completed required summarization and evaluation. Capture conversation changes without blocking the user reply path. Critical instruction/priority/goal/memory changes go through their own domain commands and must complete before admission.
- **Invariant / failures:** Ordered `summarize → evaluate → critical owner commits`; optional embeddings/reflections do not gate unrelated tasks. Generation/epoch/version check on every state commit; unfinished critical stage keeps the epoch not-ready. No model call inside SQL transaction.
- **Dependencies:** P4-01, Phase 2 range processing, Phase 3 domain commands. **Type:** production + PostgreSQL/LLM tests.
- **Acceptance:** multiple conversations, partial ranges, source corrections during inference, crash after stage commit, failed critical updates, empty safe ranges, return while inference is underway.

### P4-03 — ALIGN-01 validation, priority refresh and readiness
- **Owner:** Goal/Task and Procedure owners validate their records; Memory/Cognition assess source/evidence; Evaluation aggregates; Background coordinator admits.
- **Reuse:** goal ancestry and strategy state, task transitive dependency/priority contracts, procedure applicability/version pins, P0 `background_ready_epochs`.
- **Contracts / schema:** Change-focused dirty/work lineage inputs, owner-approved findings and versioned stage result; atomic READY insertion only once every critical required stage is satisfied. Deterministic ancestry/version/authorization checks first; targeted semantic review only when necessary.
- **Invariant / failures:** Confirmed action-critical conflicts block *affected* operations; uncertain/noncritical findings do not block unrelated safe work. No full-state LLM review each transition. Older epoch or stale input may not insert READY.
- **Dependencies:** P4-02, ALIGN-01. **Type:** production + tests.
- **Acceptance:** descendant goal drift, legitimate revised evidence, stale owner version, unaffected independent task, return during alignment check, no premature READY.

### P4-04 — Authorized bounded autonomous selection and execution
- **Owner:** Goal/Task selects and confirms actionability, Procedure Manager authorizes exact steps, P0 executes, Model/Tool Gateway enforces capability.
- **Reuse:** P0 priority/resource reservations, group locks, operation leases, task DAG and effective prerequisites, ProcedureRun staging and read-only result reconciliation.
- **Contracts / schema:** Separate tightly scoped platform/domain-internal P2 admission for previously approved action and immutable versioned step/capability; never expose generic model-generated operation creation. Add capability-specific read-only research adapters only when their permission and evidence contracts exist.
- **Invariant / failures:** GOAL origin is not permission; no new external side effects without explicit approvals, provider idempotency and cancellation rules. Completion of an operation never implies goal success. Resource caps proposed as 2 autonomous tasks and 1 LLM inference, subject to measurement.
- **Dependencies:** P4-03. **Type:** production + tests.
- **Acceptance:** authorized task advances; cancelled/unapproved/blocked task cannot; concurrent independent P2 steps; P0/P1 reservation protected; exact tool/step version and deadline rechecked.

### P4-05 — Foreground preemption, checkpoint, recovery
- **Owner:** Persona Runtime controls authority; P0 Execution handles leases, checkpoint, suspension and recovery.
- **Reuse:** `operation_control.py` heartbeat/checkpoint/reaper/resume, claim generations, `OUTCOME_UNKNOWN` quarantine and provider reconciliation.
- **Contracts / schema:** Bounded cooperative suspension signal for running P2; epoch-scoped stage recovery and finite retry/backoff; resume only after a new successful barrier and renewed task authority.
- **Invariant / failures:** Return immediately stops *new* P2 claims; old epoch cannot commit. External uncertain effects are reconciled rather than blindly retried; incomplete ephemeral cognition is discarded. No locks across tools/LLM calls.
- **Dependencies:** P4-01–04. **Type:** production + tests.
- **Acceptance:** return during work and inference; stale worker write denied; safe checkpoint then resume; exhausted bounded attempts; crash after dispatch yields quarantine, not duplicate side effect.

### P4-06 — Policy-compliant notifications and follow-ups
- **Owner:** Communication System/platform notifier; Persona cognition never activated by delivery.
- **Reuse:** durable events/outbox, existing conversation follow-up correlation, user-global settings where established.
- **Contracts / schema:** Authorized outbound intent and due-time/recurrence, timezone/DST/quiet-hour/delivery policies, authenticated response correlation, idempotent delivery. Implement only a supported channel first.
- **Invariant / failures:** Platform may deliver approved notifications while persona Inactive, but never perform persona reasoning or change goals merely through that delivery.
- **Dependencies:** P4-04 for goal outcomes, P0 ingress. **Type:** production + tests.
- **Acceptance:** duplicate callback, inactive reminder, quiet hours, missed schedule, correct source-persona correlation.

### P4-07 — Integrated Phase 4 acceptance
- **Owner:** Cross-domain QA / architecture review.
- **Reuse:** all prior test suites, existing live Ollama/PostgreSQL acceptance workflow.
- **Contracts / schema:** None expected except issues discovered under test.
- **Invariant / failures:** Foreground responsiveness, scope isolation, authorization and historical state continuity survive failures and user return.
- **Dependencies:** P4-01–06. **Type:** integration/tests only unless defects.
- **Acceptance:** PostgreSQL race/restart drills, restricted-tool live integration, adversarial permission tests, response-latency and resource budgets; independent Codex review before merge.

## Confirmed versus proposed

**Confirmed v2.5.1:** one selected instance, three lifecycle modes,
Background-only autonomy, strict summarize/evaluate/critical-commit/
change-focused-alignment/priority barrier, no second scheduler, P0/P1 precedence,
epoch/generation fencing, domain authority, bounded recovery, PERS-ALIGN-01,
ALIGN-01, and no mandatory full-state audit.

**Implementation proposals (not yet approved as architectural defaults):**
`background_preparation_epochs` schema and its statuses; how to snapshot
multi-conversation input ranges; stage/retry storage; initial P2 concurrency
and LLM budgets; exact alignment labels and semantic-review triggers; delivery
channels and cadence. Establish these incrementally with measurements.

## PR workflow

Implement one milestone or safe sub-slice per branch. Run all PostgreSQL
tests and contract checks, plus live Ollama/PostgreSQL acceptance when relevant.
Request independent Codex review; resolve findings and **never merge without
the user's explicit approval**.
