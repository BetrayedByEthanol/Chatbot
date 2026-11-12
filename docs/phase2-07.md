# P2-07 — Emotion & Relationship State

Authority: **Persistent Persona Agent Architecture v2.5** §§4.1, 4.3, 9, 17.2, 20.2–20.3, 23; and behavioral refinement R01/R03/R05 (subjective continuity cannot corroborate itself). This is a conservative, independently testable first slice. Emotion is a **simulated persona state** and any user-affect estimate is **tentative**, not verified.

## Domain ownership

**Emotion Engine** owns `persona_emotion_state` and `emotion_appraisals`; **Relationship Manager** separately owns `relationship_state`, `relationship_observations`, `relationship_preferences` and their append-only revisions. Stable persona traits, Cognition proposals, verified user memories, goals, tasks, procedures and platform permissions are **not modified**. There is no new global or cross-domain mutable state.

All persistent state is scoped to `(user_id, instance_id)`, which is the implementation's scoped persona instance for `(user_id,persona_id)`. Independent numeric versions and short domain transactions allow unrelated owners to progress concurrently. Incoming evidence must have an authenticated input-event and scoped, immutable user turn. Domain events carry source causation, trust metadata and transactional P0 outbox publication.

### Emotion

- Mood comprises valence `[-1,+1]`, arousal `[0,1]` and engagement `[0,1]`. Default neutral baselines are `0.0,0.25,0.5`; these **provisional tuning parameters** are not architecture-wide fixed traits.
- Only a narrow exact-utterance dictionary admits small changes: direct `I'm happy/sad/excited/frustrated`, gratitude and explicit helpful/unhelpful feedback. Questions, speculation, third-party or quoted content, medical statements and compound ambiguous language do **not** produce automatic appraisal.
- An expressive mood reaction is **not the user's mood**. Separate `TENTATIVE` user-affect cues are recorded only for direct supported self-reports with quote, confidence `0.82`, source timestamp and **four-hour expiry**. No cue becomes a `memory_record`, a verified fact or unreviewed Goal Engine evidence.
- Persisted mood is updated once per newer source event. Late historical source events are audited but cannot roll back newer affect. `read_emotion_state()` derives exponential decay **on read** toward baselines (provisional eight-hour half-life) without a timer, background write, or separate scheduler.

### Relationship

- Every authenticated user interaction contributes at most one durable observation to `interaction_count` and bounded familiarity. Mere message frequency/cadence does **not** imply trust, affection, anger, tiredness or attachment.
- Rapport changes only for an exact explicit gratitude/helpfulness or unhelpfulness statement; it is a soft, bounded display/salience signal and cannot grant permissions or override instructions.
- `set_relationship_preference()` requires `TrustedActor`, selected Foreground generation, genuine source-turn quote, exact expected per-preference revision, scoped idempotency key and a short atomic transaction. Replays of the **identical** original command remain valid after persona switches; a changed request with the same key is rejected. Domain revisions and P0 outbox commit atomically.
- Supported persistent preferences: `RESPONSE_LENGTH=SHORT|NORMAL|DETAILED`, `EMOJI_USE=ON|OFF`, `PROACTIVE_MESSAGES=ALLOW|DISALLOW`, `AVOID_PROACTIVE_TOPIC` (explicit user-specified bounded topic). Topic boundaries affect **unsolicited introductions** only, not direct requests. `SET` and `REMOVE` are versioned and evidence-linked.
- An intentionally tiny allowlist of **exact explicit user instructions**, such as `Please keep replies short.` and `Please don't use emojis.`, may be admitted through the authenticated event consumer. Broad free-text policy interpretation, inferred boundaries and ambiguous instructions **do not** auto-write preferences. All preferences have `privacy_class=INTERNAL`; they are not generalized factual memories.

## Event delivery and recovery

The standalone `affect_observers` worker registers **two P0 subscriptions** to `conversation.user_text.received.v1`. Each domain independently handles its own P0 delivery transaction. Its records and state version, domain event/outbox and P0 ACK commit or roll back **together**. A failure in the Emotion domain does not roll back an already committed Relationship observation (or vice versa).

The worker is bounded (`max_deliveries_per_domain=64`), SQL-only, and has **no model call, external tool, secondary GPU service or database lock across inference**. It invokes existing P0 event lease reclamation. Input belonging to an inactive persona is **deferred**, not applied, and resumes when the same persona is reselected. Late subscription recovery can use P0's explicit `backfill_subscription()`; normal subscribed deployments need no bespoke historical scanner.

Use one operator command:

```sh
# Apply ordered migrations through 0013 first.
DATABASE_URL=postgresql://... python -m persona_agent.affect_observers --poll-seconds 1

# SQL-only one-shot tick; suitable for manual validation.
DATABASE_URL=postgresql://... python -m persona_agent.affect_observers --once
```

## Foreground context and trust

`persona_affect_context_block()` is a bounded, **read-only** addition to the existing `prepare_text_context()` query transaction. It emits explicit user preferences first, then optional expiring user-affect cues (clearly marked as tentative), then optional simulated mood. It does **not** present familiarity as attachment, label the user with a permanent emotion, or invoke a classifier. If the budget is insufficient it adds nothing, preserving recent verbatim conversation and the latest user message. Direct user requests, system safety and permission policy always outrank mood or style hints.

The Emotion Engine does not accept cognitive proposals as domain commands. Later work may introduce a separate validated appraisal-proposal gate; P2-07 makes no implicit cross-domain promotion.

## Acceptance and deferrals

`tests/test_phase2_affect.py` executes real PostgreSQL cases for source scope, independent owner state, cautious parser, read-only temporal decay, explicit preference and boundary CAS/idempotency, removal/revisions, selected-persona deferral/recovery, concurrent delivery idempotency, atomic rollback after writes and before ACK, historical staleness, and bounded foreground context with preference precedence.

**Explicitly deferred:** calibrated affect models, LLM-based emotion inference, relationship trust/attachment assessment, automatic adoption of Cognitive Engine hypotheses, affect-based permissions, user mood as a trusted fact, unrequested proactive messages, and full P2-08 cross-component quality/latency evaluation. Thresholds and numeric updates are adjustable implementation parameters requiring later evaluation rather than silent v2.5 redesign.


## P2-07 review corrections

- Feedback and automatic communication-instruction matching requires source input of 200 characters or fewer. Longer inputs are recorded as neutral interactions; no oversized evidence quote is inserted.
- Automatic preference ingestion does not update a preference when its source timestamp is earlier than or equal to the current revision's evidence timestamp. Explicit commands retain version checks and can remove a preference using the same source turn.
- Context retrieves and renders the singleton communication preferences (response length, emoji use, proactive messaging) ahead of topic boundaries, preserving their query slots and their portion of the limited context budget.

PostgreSQL regression tests cover long punctuation tails, subsequent valid events, delayed automatic ingestion following explicit set/remove, and five or twenty active topic boundaries.
