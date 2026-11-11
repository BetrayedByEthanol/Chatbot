# P2-03 — Automatic Memory Scheduling

Authority: Persistent Persona Agent Architecture v2.5 §§7, 8, 19.3, 22.2, 23.1 and the P2-01/P2-02 contracts. This is an **admission-only** vertical slice, not a new inference engine, a second queue or the full Background barrier.

## Scope and ownership

Conversation owns user turns, actual delivery evidence and the contiguous summary watermark. P2-02 Memory owns source-linked summaries, episodes, and model-output validation. P0 owns subscriptions, event delivery, leases, execution resources and operations. This slice adds one trusted platform-owned scheduler consumer between durable events and P2-02 admission.

- The automatic_summaries module registers consumer memory.auto_summary.v1 for conversation.response.delivered.v1, memory.conversation_range.summarized.v1, persona.selection.changed.v1 and persona.mode.changed.v1.
- On delivered activity, a bounded policy enqueues only after at least **8** eligible turns while retaining the latest **2** user turns. Each operation summarizes up to **8** turns. These defaults are tuning parameters, not v2.5 invariants.
- On selected Background departure, it may admit shorter eligible ranges. Work remains subject to P0 scheduling. The full Background preparation barrier is not implemented here.
- Summary completion events may admit the next eligible contiguous range. Terminally failed summary operations are **not automatically re-admitted**; explicit retry_failed=True remains an operator/user decision.
- Periodic bounded reconciliation repairs missed events, switch-back cases and jobs staged before a crash. Only currently selected User × Persona instances are scanned. Inactive persona deliveries are deferred and resumed on reactivation.

## Event and crash contract

One run_auto_summary_tick() invocation:

1. Boundedly dispatches P0 outbox events and reaps expired delivery claims.
2. Resumes deferred events for currently selected foreground/background personas.
3. Claims one event at a time using P0 fenced delivery leases.
4. Checks eligibility without model inference and invokes the existing P2-02 schedule_summary() and P0 enqueue_operation() for durable admission.
5. **Only after admission commits**, acknowledges the event through P0 process_delivery(). A crash between enqueue and ACK reprocesses the event without duplicating the operation, thanks to existing idempotency.
6. Reconciles bounded eligible selected conversations. Source readiness and persona-generation fences remain authoritative in P2-02.

The two-transaction event-to-operation handoff is intentional: P0's process_delivery() prohibits independent operation transactions inside its database-only handler. No conversation or delivery lock is held during inference.

### Platform authorization

P0 enqueue accepts TrustedPlatformActor **only** for a non-effecting COGNITIVE-origin memory.summarize_range.v1 operation with FOREGROUND_ASYNC/P1 scheduling, the cognition.llm resource and memory.summarization capability. Platform actors cannot enqueue generic goals, tools, external effects or arbitrary operations. User-origin summary admission remains unchanged. All state uses PostgreSQL's selected persona generation fencing.

## Operator setup

Configure the existing cognition.llm resource pool, apply the ordered migrations through 0009 and launch an authorized worker service:

    DATABASE_URL=postgresql://... python -m persona_agent.automatic_summaries --poll-seconds 1 --min-turns 8 --retain-recent 2 --max-turns 8

For one bounded cycle:

    DATABASE_URL=postgresql://... python -m persona_agent.automatic_summaries --once

The service **only admits jobs**. A separate supervised P0 memory.summarize_range.v1 executor with memory.summarization capability and a validated summarizer gateway must execute the model operations. No new foreground LLM inference is introduced.

Register before enabling dispatch when possible. On existing deployments the reconciliation pass restores current eligible work; P0 backfill_subscription() can replay historical events explicitly if needed.

## Acceptance and deferrals

tests/test_phase2_automatic_summaries.py uses real PostgreSQL to verify threshold admission, contiguous ranges, lost-event-ACK recovery, inactive persona deferral/reselection, short Background handover and platform permission limits.

Deferred: model quality benchmarking, automatic factual promotion, full cognitive reflection, topic-boundary classification, privacy/retention propagation, the complete Background barrier, and deployment process orchestration beyond this operator-owned loop.


### Bounded reconciliation fairness

The reconciliation query filters out active/terminally admitted jobs and ranges containing currently unfinished/deliverable responses **before** applying its scan-page LIMIT. Otherwise a few persistent blocked conversations could monopolize every poll and prevent late-subscription recovery of unrelated eligible conversations. A job staged immediately before a crash but not enqueued remains visible for idempotent admission recovery. A PostgreSQL regression forces a one-conversation scan limit and verifies that an eligible range is still scheduled behind both a blocked conversation and an already queued operation.


## Codex review hardening — retry failures, origin provenance, staged retries

- **Recoverable admission failures:** The event consumer already applied bounded
  P0 retry to temporary admission failures. Periodic reconciliation now also
  catches `ResourceUnavailable` and `InvalidOperation` per conversation,
  preserving the staged job and continuing the worker loop. The next tick can
  admit once P1 pool capacity is configured; other conversations are not
  blocked by a single failed admission.
- **Immutable original origin:** Migration `0010` adds
  `conversation_summary_jobs.admission_origin` (`USER` for prior P2-02
  records, `COGNITIVE` for new platform-origin jobs). P2-02
  `schedule_summary` always reuses the staged job's original origin,
  including when a different authorized caller repairs it. P0 allows a
  *cross-origin caller* only for an exact, already staged, same-user,
  same-persona, same-generation summary job matching the canonical
  non-effecting P1 operation shape and idempotency identity. This never
  grants an alternate caller arbitrary cognitive/tool admission authority,
  and never relabels the original operation origin.
- **Latest-attempt reconciliation:** Both preflight and bounded scan select
  the newest `admission_attempt`, using a LEFT JOIN to P0 operations. An
  operator-retried job staged before a crash but missing an operation row
  is therefore repairable even when an older attempt has status `FAILED`.
  Historical failed attempts remain durable and are never retried
  automatically.

Four PostgreSQL regressions cover a temporary unavailable cognition pool,
USER/COGNITIVE replay in both directions, concurrent repair of a staged
cross-origin operation, and recovery of a newer user-authorized retry staged
after a failed automated attempt.
