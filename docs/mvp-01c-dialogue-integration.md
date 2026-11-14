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
   QUESTION pending on an automatically managed thread. Explicit
   either/or choice prompts can instead be tracked as a pending DECISION,
   **never automatically approved**. Their source is the **delivered
   response**, and neither kind can authorize a goal, tool, or procedure
   outcome. On a clear adjacent user reaction, a QUESTION may change
   to ANSWERED; a DECISION remains pending for explicit domain-safe
   resolution. Neither goals nor tasks change.
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
- A new subject request (e.g. "Help me cook dinner.") cannot satisfy a
  previous topic's adjacent pending question; it remains unlinked at ingress
  so delivery can focus/open the requested subject.
- Inputs eligible for MVP-01B model interpretation (e.g. "Lately I've been
  thinking about a regular walking routine.") also remain **unlinked at
  ingress**, because the model might propose a new OBJECTIVE at response
  commit. Delivery uses the persisted proposal, when present, to select the
  new topic without resolving an unrelated pending question. When no
  admissible signal arrives, automatic answer resolution conservatively
  abstains rather than treating ambiguous text as an answer.
- Before linking an adjacent answer, ingress also checks the already
  persisted `related_signal_id` from MVP-01A. When a supported reference
  points to a **different** thread (or has no resolved target), the input
  remains unlinked and the previous question remains pending. Delivery can
  then refocus the original referenced thread and associate any new question
  with that topic rather than the unrelated current thread.
- Automatic delivery adaptation is allowed only when conversation revision
  equals `conversation_responses.context_revision + 2`: the generation and
  delivery increments, with **no intervening explicit dialogue command**.
  An explicitly resolved thread cannot be reopened or have its next action
  overwritten by an older generated question. The response can still be
  marked delivered.
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
Additional regressions assert a new cooking request cannot answer a walking
question, and explicitly resolved progress wins over a reply generated before
that explicit change but acknowledged afterward. An additional parameterized
real-PostgreSQL regression verifies that model-eligible implicit language
does not prematurely answer a pending question, either when the model
subsequently supplies an OBJECTIVE (new topic) or supplies no reliable
interpretation (abstain).
A further PostgreSQL regression covers a cooking thread with an adjacent
pending question followed by "Please suggest the next step for that goal.",
where the `related_signal_id` references an older walking objective. The
walking thread is restored on delivery; the cooking question stays pending.

Run full Phase 0 PostgreSQL contract tests and the existing Phase 1 live
Ollama acceptance workflow. Behavior remains intentionally narrow:
- DECISION prompts are tracked only for clear either/or requests, never
  automatically resolved; a conversational yes is never goal consent.
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
