# P3-04 — Durable Procedure Runs & Conversation Progress

**Authority:** Architecture v2.5.1 §§18.2–18.4 and 21.4, P3-03 and the merged P3-02 corrections. This increment implements persisted guided procedure attempts, with no new scheduler and no task or goal write authority.

## Domain ownership and boundaries

- **Procedure Manager:** one `ProcedureRun` per approved attempt, pinned shared `template_id/template_version` and optionally pinned private `variant_id/variant_revision`. Rendered instructions, success tests, executors and capabilities are persisted as immutable step definitions; the SHA-256 snapshot pin allows reproducible recovery even after template and variant publication. The rendered-step read contract uses the same post-interpolation limits as `validate_variant` and the run snapshot schema: **1,200 instruction characters / 800 success-test characters**. Published `ProcedureStep` templates retain their tighter **800/400** limits; recovery must not revalidate rendered text against template-only limits.
- **Conversation Manager:** independent dialogue thread focus, next conversational action and actual response delivery. `procedure_run_thread_links` attaches existing Conversation-owned threads to a run across different conversations without mutating conversation attention, resolution, or turn history.
- **Task/Goal Manager:** authoritative work completion, corrections, dependencies and goal status. A run may optionally bind one currently actionable USER task, but neither recording progress nor confirming all steps changes the task. Run success is **not** goal attainment. Run admission applies the Task domain's existing `DEADLINE_EXPIRED` gate under the scoped graph lock, as well as goal/ancestor, suspension, exclusivity and prerequisite rules. An expired task may still accept truthful late reports through the Task evidence command; it cannot authorize starting new guided work.
- **P0 Operations:** no task-owned or run-owned operation admission in this slice; `EnqueueOperation.task_id` remains rejected. AGENT steps remain unexecutable and cannot be user-confirmed, independently verified, or marked attempted by a USER command.

## State and evidence

The run states are `ACTIVE`, `PAUSED`, `NEEDS_REVIEW`, `USER_CONFIRMED`, and `CANCELLED`. Steps are `NOT_STARTED`, `INSTRUCTED`, `ATTEMPTED`, `USER_REPORTED_DONE`, `USER_CONFIRMED`, `FAILED`, `BLOCKED`, `NEEDS_REVIEW`.

1. Starting a run requires authenticated source evidence plus a **separate action-bound user approval** and pins a versioned reviewed method; neither current shared template nor current personal variant follows future changes automatically.
2. Reporting ATTEMPTED, USER_REPORTED_DONE, FAILED or BLOCKED is low-risk evidence admission from a quoted authenticated user turn, not a tool execution assertion. A USER_CONFIRMED step requires a preceding USER_REPORTED_DONE state and **separate exact approval**. A run can finish only when every step is USER_CONFIRMED and none are AGENT-executed; `USER_CONFIRMED` does not mean independently VERIFIED.
3. CORRECTED events require approval and reference the **latest** step event. Prior testimony is immutable. A completed run is reopened as `NEEDS_REVIEW`; it remains in that state while **any** step still needs reassessment, including when other steps advance. Clearing the last step correction resumes ACTIVE but never automatically finalizes the run. Corrections of historical step evidence are also accepted while a run is PAUSED or CANCELLED, without resuming it or reauthorizing operations. RESUME transitions into NEEDS_REVIEW if a corrected step remains unresolved. Already confirmed independent steps remain as historical evidence.
4. `INSTRUCTED` requires a conversation response already marked `DELIVERED` and containing the exact pinned step instruction; mere text generation does not progress the procedure. An adapter-backed partial playback/interrupt channel is **not** yet implemented, so rich/voice transports must not claim hearing from text acknowledgment alone.
5. Each mutation is scoped, idempotent, generation-fenced and event/outbox-backed using the existing P3-03 procedure transaction contract. Run and step versions reject stale commands, including concurrent step updates. No model/tool calls are made under locks.

## Recovery

`read_procedure_run` reconstructs versioned status, ordered pinned steps, last step evidence and cross-conversation thread links after a restart. `list_procedure_runs` provides a bounded selected-persona recovery view. Interruption leaves unconfirmed attempts/steps explicitly incomplete. A new conversation can open its own DialogueThread and link it to an existing run; it does not create a second run or overwrite the previous conversation.

## Known deferrals

- **P3-04 operation gate extension:** agent execution would require a dedicated admission contract connecting the task version, run/step revision, selected persona, capability authorization, tool-result provenance, cancellation and the existing P0 scheduler/operation-result confirmation. This PR intentionally does not expand tool permissions or pretend verification. That work remains a separate guarded integration slice before P3-04 is considered fully complete.
- Delivery progression currently requires **literal step text** in an acknowledged text response; shorter paraphrases and interrupted/multi-clip TTS will need explicit per-step delivery metadata from the communication adapter, never an inferred acknowledgment.
- P3-05 owns outcome classification and safe method adaptation. Feedback from a run cannot automatically publish globally or revise the user-approved goal.
- Existing strict confirmation phrase UX is a provisional safety boundary, not the desired final natural-language presentation.

## Acceptance

`tests/test_phase3_runs.py` verifies run pinning after template/variant changes, recovery, no task completion, evidence/correction history and version conflicts, delivery-before-ack rejection, cross-conversation thread linkage and persona isolation, concurrent step updates, pause/resume, and AGENT nonexecution. Codex follow-up regression coverage exercises exact rendered 1,200/800 limits through both read/list APIs and the task deadline boundary (future allowed, expired rejected, late reporting still admitted). Full PostgreSQL and live integration CI are required; independent Codex review before merge.
