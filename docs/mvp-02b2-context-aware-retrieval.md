# MVP-02B.2 — Context-Aware Historical Retrieval

Authority: Architecture v2.5.1, Conversation/Memory provenance and scope.
Baseline: merged MVP-02B.1 in `main` (`392ebbd3`).

## Small, bounded behavior increment

MVP-02B.1 already retrieves raw authenticated historical **user** turns only
when the existing Memory/episode prompt paths supply no evidence. MVP-02B.2
extends that existing retrieval path; it adds neither a competing memory store,
nor new background workers, model inference, vector infrastructure or
authoritative domain objects.

1. **Independent limited paraphrases.** The narrowly defined retrospective
   question `What outdoor activity had I been considering?` is allowed as
   an explicit history lookup even though `walking` is absent from its
   wording. A small, documented exercise-activity vocabulary supplies
   PostgreSQL English-FTS search candidates (`walk/hike/cycle/jog`). The
   model receives **only source-linked historical statements**, never an
   asserted inferred activity. Check activity-category existence across
   **all** authenticated, eligible historical turns **before** imposing
   the top-16 relevance cutoff used to select quotations. This independent
   preflight streams matched turns through a PostgreSQL server-side cursor,
   applies the same sensitive-content and Memory-source exclusions, and
   stops early if two categories qualify; otherwise it scans the complete
   eligible set. If multiple activity categories match anywhere in scope,
   retrieval abstains instead of choosing from the top-ranked subset.
2. **Correction-aware local context.** For up to the three best matching
   historical source turns, inspect at most the next three *responded* user
   turns of each in that **same** conversation. Deduplicate follow-up turn
   UUIDs across overlapping windows. Only contiguous, tightly specified
   weekly-frequency notes followed by one or more explicit weekly-frequency
   corrections are eligible. The lookahead scans **all three positions**
   rather than returning after the first correction; where multiple valid
   corrections occur, the final one supersedes earlier cadence suggestions.
   Budget the correction chain atomically: never emit a tentative older
   frequency without its subsequent corrections. If space is insufficient
   for the full chain, include **only the last correction**. Read **all
   intervening sequence positions**, including `PENDING` and
   `SUPERSEDED` turns, and stop at any non-`RESPONDED` turn. This
   prevents a superseded topic switch or frequency note from being
   silently skipped when a later correction is associated with the
   original discussion. Also stop at unrelated messages, responded topic
   switches, privacy exclusions or invalid provenance. If Conversation owns a linked correction signal, require it
   to target the selected source; if a later frequency correction fails
   linkage validation, abstain from showing outdated earlier corrections.
   Include each rendered user update in chronological order, with its own
   turn UUID and date and a `USER_REPORTED` epistemic label.
3. **Preserve existing fences.** The initial candidate query still requires
   the selected authenticated `(user_id, instance_id)`, a previous
   conversation ID, input timestamp earlier than the current turn,
   `RESPONDED` status, exclusion of Memory-owned source revisions and
   extraction candidates, and conservative full-turn sensitive-data
   filtering. Optional evidence cannot displace mandatory newest input.
   Rendered quotes are discussion evidence, never permission, verified fact,
   tool outcome or an approved Goal.

## Scope and limitations

The paraphrase bridge is intentionally **not a general semantic search
engine**. Other paraphrases without recognized lexical roots may still be
missed, and a broad question with competing activities abstains. This is a
measurable vertical slice before deciding whether pgvector is worthwhile.

Unstructured corrections outside the narrow contiguous weekly-frequency
grammar are *not* automatically applied; the system must not infer the
relationship between arbitrary adjacent statements. Domain-linked
`conversation_intent_signals` remain tentative, not Goal-domain state.

As documented in MVP-02B.1, raw `conversation_turns` still lack per-turn
privacy and retention labels. Regex filtering is a conservative safeguard,
**not comprehensive sensitive-content classification**. Broad archival
recall should remain limited until a stronger source-level policy exists.

## Post-acceptance recall certainty calibration

The qwen2.5:7b MVP-02B.2 review confirmed correct retrieved evidence and
three → twice → once/week ordering, but the assistant described the user's
"once per week would be better" as a schedule they had "settled on." That
overstates the user's epistemic commitment.

The existing historical `USER_REPORTED` context heading now explicitly
instructs the model to preserve tentative wording ("maybe", "would be
better"). Corrections replace an earlier **suggestion** without implying a
final decision, approved Goal, accepted plan, or user commitment. Explicit
commitments remain distinct and must be supported by user source wording.

This is **prompt-only** calibration in the existing Conversation-owned
historical recall context, used for direct and independently paraphrased
recall. It does not mutate Memory, Goals, Conversation state, authorization
or operation scheduling, and it is not a general model-output verifier.

The PostgreSQL-backed regression checks actual assembled source-linked
model context for both recall routes; the target-model scenario's human
review prompts now explicitly reject "settled on" and similar unsupported
commitment wording. Another real-model ZIP is necessary to verify response
calibration.

### Follow-up: intermediate-correction certainty

The calibration review on `qwen2.5:7b` still said the user **"decided twice
a week"** when the source was **"Actually, twice per week would be
better."** Both the intermediate twice-weekly and latest once-weekly
statements remain preferences. The historical context guidance now applies
noncommitment wording to **every** sourced statement, explicitly rejecting
"decided", "settled on", "agreed" and "committed" absent user confirmation.
A correction changes an earlier suggestion; it does not authorize or finalize
a plan. This remains a prompt-only change, not another state subsystem.

The existing two-route PostgreSQL grounding regression checks this guidance
and all source-linked frequency turns, and the target-model scenario now
calls out the intermediate "decided twice" failure. Human review remains
necessary to confirm actual model wording.

## Tests and E2E

```sh
# Disposable migrated PostgreSQL configured via TEST_DATABASE_URL:
python -m pytest -q tests/test_mvp02b_historical_user_turns.py \
  tests/test_mvp02b2_context_recall.py
```

New integration scenarios assert: a standalone paraphrase as the **first**
new-session query; ambiguity abstention when walking and hiking both
qualify, **including a hiking source hidden behind 16 higher-ranked
walking matches**; exclusion of sensitive historical activity sources
from the ambiguity decision; chronological note and correction with source UUIDs; **two
successive corrections within the three-turn window**; latest-only
correction rendering when the optional prompt budget is tight; independent
paraphrase with correction; topic-switch and unrelated-message stop rules;
no inadvertent retrieval for ordinary exercise questions; cross-user
isolation; corrected evidence from a source displaced by a newer matching
turn; **superseded intervening topic switches and frequency notes** via
normal authenticated ingress and supersession (without test-side status
mutation); and prompt-budget conservation.

```sh
bash scripts/run_persona_e2e.sh \
  --scenario scenarios/mvp02b2_context_recall.json \
  --report artifacts/mvp02b2-review.zip
```

The target-model ZIP is necessary to judge **natural** conversational
accuracy. A green unit test or existing Phase 1 live Ollama job is not a
substitute for target-model prompt/answer review. The first new-session
question must not have a previous answer in its own conversation.

**No PR merge without explicit approval.**
