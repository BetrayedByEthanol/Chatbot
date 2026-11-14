# MVP-01A — Natural-Language Intent Interpretation

## Ownership and scope

Architecture v2.5.1 §§17–18 remains authoritative. This increment extends the existing Conversation Manager, not Goal, Memory, or authorization.

Flow: authenticated text → durable ingress → bounded lexical interpretation → evidence-linked conversation signal → scoped context assembly → existing primary conversational LLM → persisted reply.

No additional model pass, new worker, scheduler or service.

## Contracts

- Schema: migrations/0032_conversation_intent_signals.sql.
- Classifier: src/persona_agent/intent_interpretation.py.
- Ingress owner: conversation.ingest_text, within the same selected-persona transaction as input/event/outbox.
- Read context: conversation.prepare_text_context, existing prompt size budget.
- Provenance: turn, source input event, sequence, method revision.
- Epistemic status: RULE_INFERRED and UNREVIEWED; not approved user instruction, goal, permission or task completion.
- Events: conversation.intent.signal_recorded.v1.
- Links: only a single, recent objective referenced explicitly by a request or correction. Ambiguity remains unresolved.

Initial narrow cue families: OBJECTIVE, REQUEST, INSTRUCTION, CORRECTION, OBSERVATION, HYPOTHETICAL. Unsupported input produces no extra record. Confidence values are not calibrated probabilities.

The Context Builder exposes tentative discussion cues only when relevant to the current turn, without retrieving unrelated old objectives. One normal response inference produces conversation and clarification.

## Examples and boundaries

- I want to eat healthier. → tentative OBJECTIVE.
- Could we plan the next step? → REQUEST; an unambiguous recent objective may be linked.
- Yes, please help me with the goal. → REQUEST, not authorized Goal command.
- Actually, I meant that goal for next year. → CORRECTION cue, no goal overwrite.
- What if I said I want to... → HYPOTHETICAL; not a real objective.
- Don't send email. → INSTRUCTION candidate, not automatically a durable communication restriction.

MVP-01A alone must not be relied upon to enforce consequential restrictions. A clear high-authority instruction needs processing by its authoritative owner or gating affected action, as required by v2.5.1.

## Verification

Apply all SQL migrations to a disposable PostgreSQL database, then run:

    python -m pip install -e ".[test]"
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0 python -m pytest -q tests/test_mvp01a_intent.py

Verify classification and abstention; evidence, events and idempotency; relevant same-conversation links; ambiguity; isolation and restart; one model call per turn; no implicit Goal-domain authorization; bounded context.

The full PostgreSQL and real-Ollama CI gates remain in force. Lexical cues and live dialogue require further measured quality evaluation before treating them as general natural-language understanding.

## Next integration boundary

MVP-01B may add optional typed proposals extracted using the same primary model pass when deterministic cues abstain. Existing conversation revision and response freshness checks govern proposal admission. MVP-03 binds explicit user confirmation to the Goal-domain command and success criteria; model-generated text never supplies authority.
