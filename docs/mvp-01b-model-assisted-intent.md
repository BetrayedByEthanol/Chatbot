# MVP-01B — Model-Assisted Intent Interpretation

Architecture: Persistent Persona Agent v2.5.1, particularly §§17–18.
This increment extends the Conversation Manager using the existing
primary conversational inference. It does not add a service, scheduler,
domain owner, or mandatory second LLM call.

## Processing contract

1. Authenticated text ingress persists the user turn and received event,
   including `conversation_turns.source_input_event_id`.
2. MVP-01A deterministic interpretation continues to handle clear text.
3. A bounded implicit-language preflight selectively chooses the
   `generate_interpreted` gateway interface only when the deterministic
   classifier abstains. All other traffic uses unchanged `generate`.
4. Ollama receives one `/api/chat` request with JSON response mode for
   both spoken reply and optional typed interpretation.
5. The gateway validates the optional model payload. Malformed optional
   metadata is discarded without another inference.
6. `commit_text_response` applies the existing selection, turn, revision
   and conversation-status checks **before** appending any model signal.
7. An accepted signal and its event/outbox entry are committed in the
   same transaction as the generated conversational response.
8. The existing host delivers only the ordinary response text.

## Typed, nonauthoritative model proposal

`ModelIntentCandidate`: kind, evidence_quote, confidence, reference.

- Kind is one of OBJECTIVE, REQUEST, INSTRUCTION, CORRECTION,
  OBSERVATION, HYPOTHETICAL.
- `evidence_quote` must appear verbatim in the **latest authenticated
  user input** (not merely model-generated context); <=200 characters.
- Confidence is uncalibrated; candidates below 0.75 are discarded.
- Model-derived INSTRUCTION proposals are **not persisted**, because
  consequential restrictions must be handled by their authoritative owners.
- Model-supplied reference is a limited hint. The Conversation Manager
  resolves a possible recent objective from scoped database evidence,
  only when exactly one exists and the latest text contains anaphora.
- No model-supplied IDs, permissions, goal approvals, or task outcomes
  are accepted.
- Persisted evidence uses source user turn/event AND source model
  response, with `MODEL_PROPOSED` / `UNREVIEWED` status.
- These entries remain discussion context, not authoritative goal state.

An absent, malformed, hallucinated or ambiguous proposal is **not a
failed response**; the user still gets the conversational reply when valid.
A malformed structured reply itself is a model-unavailable error,
without silently retrying or sending JSON metadata as conversation.

## Coverage and limitations

A narrow deterministic trigger keeps inference/resource costs bounded:
"Lately I've been thinking ...", "Maybe I should ...", "I wish ...",
"That sounds ..." and similar cases. General-purpose paraphrase
coverage, calibration and multilingual intent accuracy are **not**
claimed. Ordinary unrecognized text still gets the normal response.

Existing MVP-01A signals remain compatible and unique per source turn.
The model route complements rather than overwrites those signals.
This is an MVP-01B implementation increment, not finished goal
approval, strategy management, or all of MVP-01.

## Acceptance

- Unit tests: `tests/test_mvp01b_gateway.py`.
- PostgreSQL behavior: `tests/test_mvp01b_model_intent.py`.
- Full PostgreSQL suite and real-Ollama integrated acceptance remain
  mandatory.
- Additional live observation: `scripts/mvp01b_probe.py`, generating an
  archived report from two ordinary messages. The probe measures
  model-proposed state and delivered replies; semantic precision needs
  larger representative human review before certification.

Do not authorize or merge derived work based only on a model signal.
