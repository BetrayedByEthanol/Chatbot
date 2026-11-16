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
   asserted inferred activity. If multiple different activity categories
   match, retrieval abstains instead of choosing arbitrarily.
2. **Correction-aware local context.** For the best matching historical
   source turn, inspect at most its next three *responded* user turns in
   that **same** conversation. Only a contiguous, tightly specified weekly
   frequency note followed by an explicit weekly frequency correction is
   eligible. Stop at unrelated messages or a topic switch. If Conversation
   owns a linked correction signal, require the link to target the selected
   source. Include each reported update in chronological order, with its
   own source UUID and date and a `USER_REPORTED` epistemic label.
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

## Tests and E2E

```sh
# Disposable migrated PostgreSQL configured via TEST_DATABASE_URL:
python -m pytest -q tests/test_mvp02b_historical_user_turns.py \
  tests/test_mvp02b2_context_recall.py
```

New integration scenarios assert: a standalone paraphrase as the **first**
new-session query; ambiguity abstention when walking and hiking both
qualify; chronological note + correction with source UUIDs; independent
paraphrase with correction; topic-switch and unrelated-message stop rules;
no inadvertent retrieval for ordinary exercise questions; cross-user
isolation; and prompt-budget conservation.

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
