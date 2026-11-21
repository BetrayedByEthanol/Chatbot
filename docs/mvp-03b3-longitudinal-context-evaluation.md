# MVP-03B.3 — Longitudinal Contextual Intelligence Evaluation

## Purpose

Evaluate the actual v2.5.1 Foreground Context Builder across repeated turns,
session interruption, goal/procedure changes, cross-user boundaries, and
long conversation histories. The evaluation measures **retrieval availability**
to the response model; it does not claim that a small Ollama CI model
understands or follows those records naturally.

## Method

Run real PostgreSQL fixtures through the unchanged
\`prepare_text_context\` and authenticated owner commands. Use fixed model
replies only to persist ordinary DELIVERED conversation turns. Observe the
system message that the real Context Builder passes to the model; retain the
latest real input separately. Reuse existing P3 Goal, P3 ProcedureRun,
Dialogue and memory acceptance helpers rather than inserting fabricated
authoritative rows.

The offline scorer (\`tests/context_quality_metrics.py\`) recognizes only the
structured Goal / ProcedureRun / prior Dialogue labels already authored by
the Context Builder. It never scores text copied from the user's input or
model replies as successful recall.

For each labelled query establish an independent expected set of Goal IDs,
run IDs and prior pending-question labels. Report:

- micro **precision** = correctly selected / all selected (irrelevance);
- micro **recall** = correctly selected / all expected (omissions);
- **clean negative rate** = unrelated/ambiguous queries with zero selections;
- per-case misses/injections, context character-budget correctness and latest
  input preservation;
- median and observed p95 Context Builder time, **descriptive only** in CI.

All zero-denominator metrics are \`null\`; negative cases cannot inflate
positive recall. The benchmark asserts a hard zero injection/omission gate
on synthetic, deterministic fixtures. Timing is reported but NOT given a
brittle environmental threshold. Security and action authority are checked
against authoritative state, not prompt text alone.

## Scenarios

1. An approved nutrition goal and a pinned Nginx TLS guided run for the same
   selected persona. More than a dozen delivered topic interruptions.
2. Three conversations: long old session, unrelated intermediate session,
   and returning session with topical and generic references. Re-use of
   stored authority with no goal/run recreation.
3. Positive selection versus unrelated topics, ambiguous references and
   intentional clean abstention.
4. User-reported procedure progress, correction/NEEDS_REVIEW, Goal pause
   and cancelled ProcedureRun. No implied verification or execution.
5. A prior linked, DELIVERED pending question preserved after an unrelated
   conversation and retrieved only for a relevant continuation.
6. Cross-user isolation and short-budget response preservation.
7. Existing independent historical verbatim-user-turn acceptance also
   continues to verify source citations and correction chronology.

## Limits / follow-on

These are synthetic ground-truth retrieval cases. They do not estimate
field accuracy, semantic recall across all natural-language paraphrases,
user-perceived continuity, real-time speech latency or real-world Task
success. Re-evaluate with a reviewed human-labelled corpus, long-horizon
user studies and target-size models before changing retrieval heuristics.
Never auto-promote an evaluation score into memory, goal, procedure or
authorization state.

No database migration, Context Builder algorithm change, scheduler,
additional inference, or unapproved architecture is introduced.
