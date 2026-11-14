# MVP-01C — Dialogue State Integration

Architecture authority: Persistent Persona Agent v2.5.1, sections 17–18.
Builds on P1-03 explicit Dialogue State and MVP-01A/B intent signals.
This is conversation progress, not a task/procedure/goal engine.

## Implementation

1. Authenticated ingress continues to commit text and tentative user-intent signals.
   Under the existing conversation row lock, the Conversation Manager can
   resolve a **single adjacent, delivered, automatically offered question**.
   A literal ACK, user question, topic switch, objective/correction,
   hypothetical, or instruction does not automatically answer a question.
2. The primary response and any model-assisted intent candidate are committed
   with normal revision, selection and evidence checks. **No thread is opened
   at response-generation time.**
3. On **acknowledged output delivery**, the Conversation Manager uses the
   validated, persisted signal from the source turn to open or refocus a
   conversation thread when the subject is high-precision:
   - explicit objective;
   - explicit request to start/help with a topic;
   - a unique existing objective directly referenced by an intent signal.
4. Topic attention is FOCUSED or RECENT independent of resolution status
   (OPEN/RESOLVED/ABANDONED). Repeating an exactly matching auto topic
   reuses its thread. An ambiguous reference is never guessed.
5. A single, bounded, delivered assistant question may be tracked as
   QUESTION pending on an automatically managed thread. Its source is the
   **delivered response**, and it cannot authorize a goal, decision, tool or
   procedure outcome. On a clear adjacent user reaction, its discussion status
   can change to ANSWERED; neither goals nor tasks change.
6. All thread changes, turn links and events/outbox are committed in the
   existing short PostgreSQL ingress/delivery transaction. No new background
   service, no additional model pass, and no database lock during inference.
7. Explicit Dialogue Manager commands from Phase 1 remain authoritative and
   operate unchanged. Automatic processing does not add pending questions
   to explicitly managed threads or reassign an input already linked by an
   explicit command. The loader and prompt context use the existing
   `load_active_dialogue` and `prepare_text_context` implementations.

## State

Migration `0034_automatic_dialogue_progress.sql` adds only provenance markers:
- `dialogue_threads.origin_kind`: EXPLICIT / AUTO_SIGNAL.
- `dialogue_pending_acts.origin_kind`: EXPLICIT /
  AUTO_DELIVERED_QUESTION.

All existing rows remain EXPLICIT. State stays conversation-scoped,
evidence-linked, revisioned and owned by Conversation Manager; no
authoritative goal/task/procedure state is modified.

## Evidence and invariants

- Replayed deliveries cannot create duplicate threads or questions.
- Old replies delivered after a newer input cannot refocus dialogue.
- No generated-but-undelivered response can create pending questions.
- Discussion topics and QUESTION/ANSWERED records never mean goal acceptance,
  permission, instruction execution, verified completion, or success.
- The latest user message is never dropped for optional dialogue metadata.
- Only the currently selected Foreground persona may advance state.
- Natural-language reference resolution is conservative and limited to known
  recent, unique objective evidence; ambiguous references are left unresolved.

## Verification and known limits

`tests/test_mvp01c_dialogue.py` exercises authenticated ingress through the
normal acceptance host, automatic focus, pending questions, answer linkage,
restart, topic switch, repeated objective, model-originated objective, manual
thread ownership, delivered-only semantics and no domain side effects.

Run full Phase 0 PostgreSQL contract tests and the existing Phase 1 live
Ollama acceptance workflow. Behavior remains intentionally narrow:
- No automatic DECISION resolution; a conversational yes is never goal consent.
- A broad or implicit topic without a high-precision signal can continue in
  recent conversation but is not guaranteed a new persistent thread.
- No fuzzy topic identity matching across weeks/sessions.
- QUESTION detection uses a bounded punctuation gate; complex multi-question
  replies or embedded code do not create automatic pending questions.
- Question-answer recognition is a **conversation exchange observation**,
  not evidence that the answer was substantively adequate.
- The MVP-01B real small-model signal quality gap remains observable, not
  assumed solved by this integration.

These bounds are deliberate until longitudinal human-reviewed benchmarks
justify richer interpretation. The next focus should be precision/coverage
evaluation, clarifications, and explicit domain handoff—not generic
per-turn state-update LLM calls.
