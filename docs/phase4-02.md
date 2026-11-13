# P4-02 — Strict Background preparation (first executable slice)

**Architecture:** Persistent Persona Agent Architecture **v2.5.1**, §§3, 4, 7–10, 14, 20 and ALIGN-01. **Parent:** P4-01a, PR #27. This is a stacked incremental PR; do not merge it ahead of its parent.

## Current result and boundary

This increment implements the **frozen input, required summary and evaluation stages** and an explicit fail-closed critical-update handoff. It **does not** claim to implement the subsequent domain-owned critical commits and ALIGN-01 review. Those require separate validated integration with Goal, Task, Procedure and Memory owners before P0 can mark `background_ready_epochs` ready. In particular, **a completed cognitive inference or a generated intention is not a critical command receipt**.

## Existing infrastructure reused

- `lifecycle.enter_background`: selected persona selection-row lock; `background_preparation.begin_background_epoch` snapshots all current conversation tails in that **same** transaction. Primary ingress takes the shared selection lock, so new user turns cannot move the snapshot boundary during entry.
- `summary_memory.schedule_summary`, `cognitive_engine.schedule_cognition`: P2 staged jobs, frozen range digests, model validation, idempotent enqueue/commit, distinct watermarks and one P0 operation scheduler. A special `retain_recent=0` is permitted only to `TrustedPlatformActor(source_id="background.preparation")` while selected in Background. Ordinary Foreground consumers retain their existing thresholds. No LLM inference occurs in the coordinator.
- P0 leases, resource pool (`cognition.llm`), per-conversation concurrency groups and generation fences remain authoritative. No new scheduler, capability registration or goal authority is created.
- **Full-tail epoch revocation:** A handover summary keeps the epoch recorded at staging, but its P0 policy is `FOREGROUND_ASYNC`, which does not itself fence Background epochs. The Summary owner therefore checks the selected Background mode, exact original generation/epoch and `PENDING` epoch record during **both** `prepare_summary` and `commit_summary`. Selection and epoch row locks serialize with Foreground return; a return between preparation and model completion invalidates commit, so no summary, episode, or watermark is written. An earlier epoch cannot revive when the same persona re-enters Background. A revoked handover also cannot monopolize its start-sequence/generation admission slot: `schedule_summary` allocates a fresh admission attempt, `background_coordinator` ignores obsolete epoch jobs, and ordinary automatic summarization excludes revoked handover work from its preflight. Old operations remain fenced and owned by P0 for cancellation/lease recovery.
- `background_ready_epochs` stays **empty** until an owner-validated downstream stage adds it in P4-03. This is a safety gate, not a work completion metric.

## Schemas and state

Migration `0022_background_preparation_stages.sql` adds `preparation_stage` and `preparation_updated_at` to the existing P4-01 epoch. A new `background_preparation_conversations` table captures `(user_id, instance_id, background_epoch, conversation_id, target_sequence)`; `target_sequence` is inclusive and immutable, including zero for empty conversations.

Stages are `SUMMARIZE → EVALUATE → CRITICAL_PENDING`, with terminal `BLOCKED`. Epoch `status='SUPERSEDED'` overrides every stage. The coordinator changes stages transactionally with corresponding journal/outbox events. It advances only when **all** captured conversations have the corresponding watermark >= their frozen target; it does not substitute model proposals for authoritative domain commits.

## Step contract

`advance_background_preparation(connection, platform_actor, instance_id, epoch)`:

1. Requires an idle PostgreSQL autocommit connection and the trusted coordinator actor.
2. Locks the active selection `FOR SHARE`, then the matching PENDING epoch `FOR UPDATE`. Rejects an inactive/stale generation or epoch.
3. Reads only immutable per-epoch source bounds and the current domain-owned summary/cognitive watermarks.
4. If the current stage has unprocessed turns: checks existing staged job/operation status. Active work is awaited; a pre-enqueue staged job is repaired through its owner; a terminal job that failed to advance its watermark blocks the epoch.
5. Calls `schedule_summary(... retain_recent=0)` or `schedule_cognition(... retain_recent=0, min_closed_turns=1)` **after releasing all database locks**. The owning P2 worker handles inference and persistence. Pending undelivered responses keep the stage waiting, never silently skipped.
6. If all required watermarks have advanced, emits a stage-change event and progresses to the next stage. `CRITICAL_PENDING` is explicitly a handoff, **not** admission to autonomy.

`run_preparation_scan` is a bounded polling wrapper; the `background_preparation_worker` CLI only invokes this SQL coordination and deliberately performs no model inference. It is not an independent operation scheduler. Existing summary and cognition workers must be operating for progress.

### Operator execution

After applying ordered migrations through `0022`, configure the existing `cognition.llm` resource pool. The prior `automatic_summaries` worker **only admits jobs**, not inference, so this slice also supplies a small standalone P0-claimed summary inference runner. Start the P2 cognition inference worker and, optionally, its regular auto-admission worker. Start the coordination worker with:

```sh
DATABASE_URL=postgresql://... SUMMARY_MODEL=qwen2.5:0.5b \\
  python -m persona_agent.summary_inference_worker
DATABASE_URL=postgresql://... COGNITIVE_MODEL=qwen2.5:0.5b \\
  python -m persona_agent.automatic_cognition
DATABASE_URL=postgresql://... python -m persona_agent.background_preparation_worker --poll-seconds 1
```

For a SQL-only preparation tick, use `python -m persona_agent.background_preparation_worker --once`. The model inference workers use the **same** P0 operations and resource pool (not another scheduler).

No P2 goal work will start from this command.

## Explicit remaining P4-02 work

- Implement a durable, domain-owned **critical instruction/correction/priority/goal update acknowledgment** contract and consumer progress evidence for the snapshot range; the Cognitive Engine must not commit those changes.
- Reconcile source updates, mailbox M0/M1 restrictions, memory-extraction conflicts and action-critical goal/task updates. Only confirmed owner commits can satisfy critical stages; unknown or failed action-critical updates must block affected execution. Do not mark READY from a generic platform boolean.
- Make the handoff to P4-03 change-focused ALIGN-01 review explicit, with independent epoch/selection/version fencing and no full-state LLM audit. P4-03 alone may certify `background_ready_epochs` after all critical work and alignment are accepted.
- Extend integration tests for crash recovery, concurrent activity and critical owner commands before calling P4-02 complete.

## Current acceptance

Real PostgreSQL tests: scoped frozen tails, two-conversation sequential summary/cognition, no-turn case, pending undelivered response, failed operation blocking, switch/foreground supersession, actor and user isolation and no P2 readiness. Existing P0–P3 regression suites must remain green. A live Ollama/PostgreSQL acceptance workflow and independent Codex review are required before merge.

**No architecture decision is changed:** stage labels and full-tail policy are P4-02 implementation choices, not permanent defaults.
