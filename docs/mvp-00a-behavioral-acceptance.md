# MVP-00A — Longitudinal Behavioral Acceptance Harness

## Purpose

Establish a repeatable, observational baseline for persona intelligence through
the **existing** authenticated text host, selected persona and PostgreSQL state.
This is an MVP acceptance checkpoint under Architecture v2.5.1, **not** a new
runtime subsystem or a claim that the full intelligence loop works.

The harness in `src/persona_agent/behavioral_acceptance.py` reuses
`LocalAcceptanceSession`, `HostIdentity`, the existing event delivery and
explicit-memory extraction worker. The optional model baseline uses
`OllamaChatGateway`, cognitive scheduling and inference through their normal
interfaces. It creates **no** goals, memories, reflections, intentions or
emotional changes as testing shortcuts.

## Deterministic contract tests

On a disposable PostgreSQL database with applied migrations:

```bash
python -m pip install -e ".[test]"
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0
python -m pytest -q tests/test_mvp00a_behavioral.py
```

The tests cover:

- Two distinct conversation sessions with one authenticated user and persona.
- Ordinary text ingress and durable, acknowledged responses.
- Host restart and command idempotency without duplicated user turns.
- Automatic memory extraction from a normal user message, with provenance
  pointing to the original conversation turn.
- Read-only, identity-scoped inspection of authoritative PostgreSQL tables.
- No artificial goal creation when the user expresses and approves an objective.

The mock model verifies **contracts only**. An echoed reply is not evidence that
the persona understood the user, used memory or formed meaningful intentions.

## Real-model observational baseline

After provisioning a disposable database, applying the repository's SQL
migrations and starting a local Ollama model:

```bash
export ACCEPTANCE_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/persona_mvp
export ACCEPTANCE_MODEL=qwen2.5:0.5b
export ACCEPTANCE_OLLAMA_URL=http://127.0.0.1:11434
python scripts/mvp00a_baseline.py --report mvp00a-report.json
```

Optional cognitive scheduling and one authorized model inference operation per
session (requires more compute and may surface schema failures):

```bash
python scripts/mvp00a_baseline.py --cognition --report mvp00a-cognition.json
```

The real-model path runs 13 natural turns across six fresh conversations of the
same selected persona: goal discussion/approval, preferences, progress, obstacles,
adaptation and restart continuity. It invokes the **existing** explicit-memory
worker. The complete synthetic dialogue and stable response identifiers are
included in the optional report for qualitative review.

The automatic report only grades directly observable **persistence contracts**.
Conversational coherence, goal interpretation, recall quality, reflection
usefulness, emotional expression, and proactive intention utilization require
human or independently validated behavioral evaluation. Failed or skipped
cognitive inference is reported explicitly, never interpreted as successful
reflection.

## Baseline expectations and current known limitations

These are **source-inspection expectations**, not claimed runtime results:

| Capability | Current source-level finding | MVP follow-up |
|---|---|---|
| Text persistence and restart | Existing host and conversation contracts | Check again on real model |
| Explicit memory extraction | Existing allowlisted statements via event worker | MVP-02 broad natural extraction |
| Relevant personal-memory retrieval | Scoped retrieval exists, behavior not fully verified | MVP-02 |
| Natural goal approval | Text host has no natural-language goal-domain command route | MVP-01/MVP-03 |
| Reflective cognition | Asynchronous domain exists; end-to-end conversational impact unverified | MVP-04 |
| Intention utilization | Proposals are not automatically conversational commitments | MVP-04 |
| Emotion and relationship continuity | Domain state exists; behavioral effect needs review | MVP-05 |
| Full 40–60-turn longitudinal acceptance | Not exercised in this first baseline | MVP-06 |

Do **not** fail the mandatory correctness suite solely because a capability
planned for later MVP milestones is absent. Do not promote generated response
text, model guesses or a test-created domain entity to authoritative evidence.

## Safety and scope

Fixture setup may provision a user and persona using `seed_local_demo`.
The harness never issues domain mutation commands except through ordinary
conversation or an existing authorized worker. State observation uses a
PostgreSQL `READ ONLY` transaction with `(user_id, instance_id)` predicates.
No time travel is introduced in MVP-00A because its first scenario has no
time-sensitive assertions; later scenarios must use a tested injected clock
rather than tampering with database timestamps.

A new conversation ID represents a subsequent session; it does not reset the
persona. The host restart recreates transport objects, not the database. The
worker tick creates a fresh connection and performs normal durable event
processing. Full process-crash/lease recovery remains covered by the existing
Phase 0–4 tests and can be incorporated into later longitudinal scenarios.

## Recommended MVP-01A

Integrate one bounded, validated interpretation path at the Conversation
Manager boundary:

`User states an objective → selective intent interpretation →
context-aware clarification → validated goal-discussion proposal`.

Start with explicit objective/clarification signals, correction handling and
selective tool routing. MVP-01A does **not** directly create or approve goals:
MVP-03 will route an authorized confirmation through the existing Goal owner.
Keep optional rules/classifiers selective; preserve one main interactive model
inference and all authority/version guards.
