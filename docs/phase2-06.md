# P2-06 — Asynchronous Cognitive Engine

Authority: *Persistent Persona Agent Architecture v2.5*, especially §§4.2, 7.2, 8, 9, 20.1, 22.1–22.3 and 23.1–23.2. This is a bounded implementation slice of the existing Cognitive Engine, not a separate global autonomous agent or hidden chain-of-thought store.

## Ownership and trust boundaries

The Conversation domain owns immutable user turns, delivery state and the new independent `cognitive_watermark`. P0 owns durable operation admission, capability-based P1 resource reservation, leases, idempotency, selection-generation fencing, cancellation and recovery. The Cognitive Engine owns **MODEL_INTERPRETED** source-linked observations, hypotheses, tentative conclusions, open questions, self-evaluation findings and *proposed* conversational intentions. Memory, Goal, Emotion, Relationship, Task and Procedure owners retain all authority over their own state.

All cognitive output is lower-trust interpretation, not a verified user report, a goal evaluation, a plan to execute, or a stream of internal token-by-token reasoning. Every reflection is explicitly `privacy_class=INTERNAL` and records a validated `CURRENT`, `HISTORICAL` or `FUTURE` temporal scope; it is **not** eligible for ordinary NORMAL-privacy historical recall. User-source evidence must remain independently validated before any future domain-owned adoption. Identical normalized reflections across separate ranges are **linked** to an earlier reflection using a content fingerprint while retaining each distinct authenticated user-turn evidence quote. Model repetition does not independently corroborate the claim or raise its epistemic status. The worker **never** writes to `memory_records`, goal state, relationships, emotion, user preferences, tasks, procedures, permissions or platform tool authorization. No cognitive proposal is injected into response context without an additional owner-approved retrieval path. P2-07 and later attention/response scheduling remain the owners of affect, boundaries, and whether/when a proactive intention might actually be spoken.

## Durable processing contract

1. The independent scheduler subscribes to delivered/superseded conversation events and persona-selection/mode changes. It also performs bounded selected-persona reconciliation for missed subscriptions, restarts, and staged-before-enqueue crashes. Inactive persona event deliveries are deferred rather than free-form interpreted. A stale delivery lease is left for P0 reaping/reclaim, never acknowledged via an invalid claim.
2. **Stage first:** `schedule_cognition()` selects an active persona, locks the conversation for short range admission, and freezes a contiguous range from `cognitive_watermark+1`. Default Foreground minimum is **3 eligible closed turns**, retaining the most recent turn; max batch **8**. Background handover can admit a single eligible turn. These are provisional, benchmarkable policy values, not a modification of v2.5 architecture. Pending turns or current-generation generated-but-not-delivered replies block admission.
3. **Enqueue second:** a separately committed `cognitive_jobs` record authorizes an exact P0 `cognition.reflect_range.v1` operation with `COGNITIVE` origin, `FOREGROUND_ASYNC` policy, P1 priority, `NONE` effects, one `cognition.llm` resource, `cognition.reflection` capability, and per-conversation serialization. P0 verifies that staged job and shape. An attempt cannot use forged cognitive authority to enqueue arbitrary tools or goal work.
4. **Claim and prepare:** the existing P0 worker claims an operation with a fenced, expiring lease and resource reservation. `prepare_cognition()` reads a bounded source snapshot and SHA-256 digest. Only a **DELIVERED** assistant message may appear in that source; generated-only responses never masquerade as delivered.
5. **Infer outside database transactions:** `run_cognition()` calls the existing `TextModelGateway` or an Ollama adapter, with a capped JSON output schema. A pass can legitimately return an empty list if there is no meaningful interpretation.
6. **Validate and commit:** at most five typed proposals each require an existing in-range `source_turn_id` and a verbatim user-input `evidence_quote`. Missing sources, malformed JSON or duplicated outputs are rejected. A short transaction re-checks P0 lease, persona generation, frozen-range digest and cognitive watermark **after acquiring relevant locks**. It atomically appends proposals, optional non-deliverable intention records, its event/outbox and the new watermark. No LLM/tool call occurs under locks.
7. **Complete:** existing P0 operation completion follows commit. If a worker crashes after cognitive commit but before P0 completion, a later valid claim reads the committed job and replays without additional inference. Terminal P0 `FAILED` operations are never retried by ordinary scheduling. A separately authorized `TrustedPlatformActor(source_id="cognition.operator")` may call `schedule_cognition(..., retry_failed=True)` to stage a **new monotonically numbered attempt** over the original frozen range; this preserves the failed attempt's audit history and never reuses its idempotency fingerprint.

A newer user turn arriving while inference runs does not, by itself, invalidate a frozen earlier range; the result is a **proposed, processing-watermarked interpretation**, not a mutation of later user facts. A selection-generation change or modified source range does invalidate the pending commit. Future evaluation should link superseded interpretations explicitly and calibrate confidence using independent evidence.

## Proposed intentions are inert

An `INTENTION` proposal needs a source, short trigger phrase and bounded expiry (1–30 days). `cognitive_intentions` starts as `PROPOSED`, `already_asked=false`, and carries delivery constraints that require Attention Manager and Response Scheduler approval. It **does not** send a message, wake an inactive persona or mutate goals. No agent-generated belief is allowed to corroborate itself without new authenticated evidence.

## Operator setup

Apply ordered PostgreSQL migrations **through 0012**, including `0012_cognitive_engine.sql`. Configure the existing `cognition.llm` resource pool with non-P0 capacity; do not create a second scheduler or dedicated database.

Run the operator-controlled worker using an authorized Ollama model:

```sh
DATABASE_URL=postgresql://... COGNITIVE_MODEL=qwen2.5:0.5b \
  python -m persona_agent.automatic_cognition --poll-seconds 1
```

One SQL-only admission/reconciliation tick without inference:

```sh
DATABASE_URL=postgresql://... \
  python -m persona_agent.automatic_cognition --once
```

The live worker owns two independently bounded stages: P0 event/SQL admission and one claimed cognitive operation. Foreground conversation can continue without waiting for either.

## Scope limitations and acceptance

This slice does not implement cross-range fuzzy deduplication, hypothesis truth assessment, emotion or relationship updates, automatic goal/procedure changes, autonomous proactive delivery, Background barrier evaluation, or long-horizon performance calibration. Those remain future owner-domain contracts, not implicit permissions.

PostgreSQL acceptance tests cover safe range staging and closed-response selection, evidence-linked/model-interpreted proposals, inactive and switched persona fencing, operation resource claims, source/lease staleness, invalid model evidence, separate cognitive vs summary watermarks, staged enqueue recovery, committed-before-completion recovery, user-scope isolation and concurrent admissions. No tests treat model-generated confidence as verified factual certainty.
