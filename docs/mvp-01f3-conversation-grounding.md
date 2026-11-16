# MVP-01F.3 — Conversation Grounding & Final Acceptance

Architecture: v2.5.1; retains Conversation ownership and the selected
Foreground persona's `(user_id, persona_id)` isolation. No new memory tier,
Goal-domain write, tool authority, scheduler, schema or LLM pass.

## Findings from the merged MVP-01F.2 28-turn Qwen evaluation

The `mvp01e-review.zip` run on commit `957b1658ce55` passed 69/69
deterministic checks, but human review found:

- A fresh session's "Do you remember what we discussed about walking?"
  received a fabricated story about walking posture/stride. The model had
  no supported historical evidence for that claim.
- A later summary repeated that invented detail from the assistant's own
  earlier output and confused walking yesterday with skipping today due to
  rain. Original user statements in that session were available.
- "Let's discuss my walking plan instead" in a new Nginx-focused
  conversation received a verbal topic change, but no walking thread
  was focused. There was no session-local walking objective to resolve.

These are human-reviewed failures, not contradictory contract results.

## 1. Grounded historical recall and summaries

The existing `prepare_text_context()` now adds a bounded, read-only
`conversation_grounding.grounding_context_block()` **only** when the
latest user message is a historical recollection or summary request.

- For recollection, include only matching **verbatim user statements**
  from the current conversation's bounded recent context; if none
  match, explicitly tell the model to acknowledge uncertainty unless
  other source-linked memory appears in the separately retrieved
  context. Never infer absence of all past conversations from this view.
- For summaries, include up to ten recent verbatim **user** statements.
  The model is told not to attribute assistant suggestions/inventions to
  the user or erase corrections/temporal distinctions. User statements
  from "I walked yesterday" and "it rained today, so I skipped" cannot
  be silently replaced by the assistant's imagined walking-posture story.
- Labels describe this evidence as current-conversation information,
  **not** cross-session recall, approved goals, verified external events
  or instructions. It is short, bounded by the existing context budget,
  and makes no additional model/DB write.

This is prompt-grounding and provenance preservation, **not a guarantee
that a model cannot hallucinate**. User-visible accuracy remains a human
acceptance criterion; cross-session personal memory retrieval is MVP-02.

## 2. Explicit new-session topic switches

A narrowly recognized explicit request like
`Let's discuss my walking plan instead.` is now allowed to start a new
`AUTO_SIGNAL` discussion thread (title `walking plan`) **when no
same-conversation walking discussion exists**. This is a new discussion,
not recovery of a prior plan, and it does not create or approve a Goal.

- If a unique current-conversation OBJECTIVE is already linked, the
  existing reference resolver refocuses it on delivery (MVP-01F.2).
- If one or more matching threads exist but a named reference was not
  uniquely resolved, **abstain** rather than invent a duplicate or guess.
- If there are no matching current-conversation threads, a clear switch
  creates the requested discussion subject.
- A question like `What about that walking goal?` remains a reference,
  **not** an instruction to create an unavailable thread.

Thread opening and focus still happen after the reply is *delivered*,
with the Conversation Manager's selection/revision and source-input
checks. Pending questions, stale responses and explicit user-managed
topics retain existing precedence.

## 3. Regression and behavioral acceptance

New `tests/test_mvp01f3_conversation_grounding.py` exercises:
- Source-attributed current user statements, correct yesterday/today
  distinction and abstention on an ordinary input;
- Historical recall in a **new conversation** with no available earlier
  walking evidence, and a later summary after a fake model has uttered
  an unsupported posture claim;
- Cross-session explicit switch from Nginx to a new walking discussion,
  while confirming source references do not cross sessions and no Goals
  or Operations are created.
- The existing MVP-01F.2 ambiguous same-session reference tests must
  continue to pass; no duplicate topic on ambiguous reference.

`scenarios/mvp01e_conversation.json` now asserts that the explicit
new-session switch focuses walking, and adds human-review prompts for
source-grounded summary accuracy and correct acknowledgement of unknown
historical details.

### CI and final acceptance

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0
python -m pytest -q tests/test_mvp01f3_conversation_grounding.py \
  tests/test_mvp01f2_conversation_intelligence.py \
  tests/test_mvp01f1_acceptance_diagnostics.py
```

Full PostgreSQL and real-Ollama CI must pass on the PR head. Once merged,
rerun the actual 28-turn `qwen2.5:7b` evaluation and review
`review_items.json` and the last summary response. Green automated checks
are necessary but not sufficient. Only close MVP-01 after the model stops
inventing historical facts and the summary accurately describes the user
reports (one walk yesterday, skipped a walk due to rain today).
