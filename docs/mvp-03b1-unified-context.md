# MVP-03B.1 — Unified Contextual Intelligence

This is a first vertical slice, not the completion of MVP-03B.

## Existing contracts retained

The Conversation Context Builder already assembles delivered turns, active
dialogue, summaries and episodes, selective historical memory, tentative intent,
and explicit relationship preferences with bounded expressive affect.
Goal records and evidence remain Goal-owned; Cognitive reflections remain
lower-trust, evidence-linked interpretations. No new database schema,
scheduler, model call, event stream or state owner is introduced.

## Selection and boundaries

- After the established Foreground selection/turn fence, read a bounded set
  of ACTIVE goals with recursively ACTIVE ancestors, scoped to user + persona.
- Use cheap topical matching before adding goals. A generic singular
  personal Goal reference is valid only when the active root is unique.
  Unrelated ordinary conversation should not receive Goal context.
- Preserve approved/delegated authority and goal source identity.
  Source-linked goal evidence is USER_REPORTED, UNREVIEWED and not verified.
- After Goal relevance is established, optionally include one current,
  topic-matching, high-confidence HYPOTHESIS, with source-turn ID, quote
  and explicit model-interpretation warning. Never insert unapproved
  INTENTION records, SELF_EVALUATION, rejected, historical, or low-confidence
  reflections as conversational directives.
- Treat every quote and model interpretation as DATA, not a command.
  Context cannot grant tool access, propose a new approved goal, mark
  progress complete or bypass communication and privacy boundaries.
- Bound the optional block; preserve latest user input, persona authority
  and existing context before optional historical retrieval.

## Acceptance

Real PostgreSQL tests cover relevant and irrelevant Goal selection,
user-reported evidence, eligible and excluded reflections, user scoping,
read-only behavior, and prompt budgeting.

## Follow-on slices

Improve semantic relevance with trace-based evaluation; integrate
ProcedureRun and DialogueThread resume state; evaluate episodic/reflection
selection and latency on long-running conversations. Do not add new
persistent context stores until existing domain-owned read views prove
insufficient.
