# MVP-01F.2 — Targeted Conversation Intelligence Fixes

Architecture authority: Persistent Persona Agent Architecture v2.5.1,
MVP-01F.1 observational diagnostics, merged MVP-01A–E contracts.
This patch remains inside the existing modular monolith and Conversation
Manager. No model-planning step, second inference, goal authorization,
tool execution rights, schema expansion or persistent global state is added.

## 1. Objective interpretation — bounded source-grounded fallback

A real Qwen2.5:7b evaluation replied naturally to
`I've been thinking about walking more regularly.` but omitted the
optional typed OBJECTIVE. This utterance already selects the one-pass
`generate_interpreted` route.

- Continue invoking the normal single model request.
- If an admissible model signal was persisted, keep it unchanged.
- Otherwise, the *authenticated input* may produce a narrow
  `mvp01f2.routine_fallback.1` `RULE_INFERRED` `OBJECTIVE` signal
  for explicitly self-referential recurring-activity expressions such
  as `I've been thinking about walking more regularly.`
- Accept only small, anchored patterns; abstain on generic musings,
  hypothetical/negated input and discussion of other people.
- Persist the tentative signal/event in the same selection- and
  revision-fenced conversation-response transaction, using the existing
  Conversation owner insert/event path and source-input event.
- The model's candidate/validation outcome and the independent rule
  fallback remain separate in `interpretation_diagnostics.json`.
  No inferred goal is approved or created.

This is intentionally **not** general semantic intent understanding;
the next human evaluation determines whether additional cases merit
model/prompt adaptation rather than many hardcoded expressions.

## 2. Frequency details, corrections, named topic references

Conversation intent signals can now associate bounded frequency proposals
(`Maybe three times per week.`) and corrections
(`Actually, twice per week would be better.`) with **one recent, active
OPEN AUTO_SIGNAL objective**. They remain unapproved conversational
observations/corrections; neither changes Goal state nor asserts exercise
completion.

Named references such as `What about that walking goal?` and
`Let's discuss my walking plan instead.` resolve only if exactly one
existing conversation-local, source-linked OPEN objective contains the
explicit topic word. The reference uses the existing
`related_signal_id → dialogue_turn_links → dialogue thread` path and
already-established delivery-adaptation fencing to restore focus.

Two matching objectives, other sessions, closed/explicitly managed threads,
or unresolved references lead to abstention. No fuzzy embeddings, global
search, or model-generated thread IDs are accepted. Existing scope,
revision, one-selection, delivery and provenance checks remain authoritative.

## 3. Capability-grounded responses

A small, **read-only** `capability_claim_context` describes the currently
registered direct capabilities only when the user asks whether the agent can
execute, install, configure, delete or perform another external action.
It explicitly distinguishes instructions from actual shell/SSH/remote/data
deletion authority and reminds the model that a hypothetical is not approval.

The context derives registered names/effect classes from the **existing**
`tool_contracts.CAPABILITIES`; it grants no capability, inserts no tool
operation, and does not use model output for tool admission.
Ordinary conversation gains no extra prompt overhead.

This is **prompt grounding, not a deterministic guarantee of wording**.
Human review is still required for claims about running Nginx commands or
deleting user data. The behavior scenario already tests that no operations
were actually admitted and records response claims separately.

## 4. Regressions and limits

`tests/test_mvp01f2_conversation_intelligence.py` exercises:

- One-pass model omission followed by source-grounded objective persistence.
- Frequency proposal/correction linkage, cooking topic change and restored
  walking topic after host restart.
- Two matching objectives => ambiguous reference does not guess.
- Correction while focused on unrelated cooking => no false linkage.
- Capability inventory context only on relevant requests, no new operations.
- Zero approved goals and no extra inference.

The merged MVP-01F.1 tests now expect the narrow fallback when the
original walking utterance is used; other malformed, untrusted and
nonmatching model proposals remain rejected by the preexisting guard.

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0
python -m pytest -q tests/test_mvp01f2_conversation_intelligence.py \
  tests/test_mvp01f1_acceptance_diagnostics.py \
  tests/test_mvp01c_dialogue.py \
  tests/test_mvp01b_model_intent.py
```

The repository's full PostgreSQL contract suite and real-Ollama workflow
are mandatory PR checks. After merging, rerun the same 28-turn
`scenarios/mvp01e_conversation.json` with the target Qwen model, upload
the ZIP and evaluate real responses plus persisted state. Known gaps in
cross-session memory are MVP-02, and broader lifecycle evaluation remains a
separate Phase 4 acceptance task.

**Definition of done:** No new goal/tool authority; passing deterministic
regressions; 28-turn report demonstrates actual focus and intent links; no
unverified execution/deletion claim in human-reviewed responses. Passing
unit/integration CI alone is not final MVP-01 acceptance.
