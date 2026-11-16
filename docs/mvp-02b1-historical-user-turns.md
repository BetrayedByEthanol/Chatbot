# MVP-02B.1 — Authenticated Historical User-Turn Recall

Architecture authority: v2.5.1 Conversation and Memory ownership. Baseline:
merged MVP-02A strict XFAIL on `main`.

## Implementation

- `historical_user_turns.py` provides a deterministic, read-only,
  source-linked fallback for **explicit recollection** queries with
  topic-bearing terms. It retrieves *only* `conversation_turns.input_text`
  from earlier conversations of the same authenticated
  `(user_id, instance_id)`; it never retrieves generated assistant replies.
  Retrieval operates inside `prepare_text_context()`'s already-fenced
  transaction, with no extra model inference or persistent write.
- Existing `contextual_memory_block()` and `episode_context_block()`
  retain precedence. Fallback only runs if both return no evidence.
  Its 3-hit/1100-character budget, 16-row candidate limit and cheap
  explicit-recall gate avoid unconstrained context growth.
- The SQL uses bound parameters, a status gate
  (`conversation_turns.status='RESPONDED'`), exact user/persona scope,
  *different conversation ID*, index-backed English stemming and ranked
  source dates. Historical turns cited in any `memory_revisions` row are
  excluded to avoid bypassing Memory's corrections, retractions or privacy
  decisions; a matching Memory record must go through Memory's normal view.
- A rendered quotation is labeled `USER_REPORTED` and includes the original
  immutable turn UUID and source date. It is historical evidence of what
  was said—not verified current truth, a Goal, a procedure or authorization.
  Model-facing instructions explicitly prohibit following quoted instructions.

## Privacy and limitations

**Raw conversation turns do not have per-turn privacy/retention labels.** This
slice therefore suppresses known sensitive categories and email/phone-like
identifiers before putting prior-session raw user turns into a prompt. The
heuristic is deliberately conservative; it is **not** sufficient to guarantee
detection of arbitrary personal secrets or hidden sensitive details. Fully
general archival recall requires a source-level privacy/retention contract
before expanding the retrieval categories.

Unstructured natural-language corrections that were never consolidated into
Memory cannot yet be reliably linked to their predecessors. Retrieved
statements are explicitly dated and must not be asserted as current facts
without considering corrections. No semantic vector similarity, implicit
proactive retrieval, or cross-session conversation-summary search is claimed.

A paraphrase without shared lexical roots (e.g. `outdoor activity` for
`walking`) is **not** expected to be retrieved in this slice.
The real-model behavioral review still decides whether a recalled quote is
used faithfully. Test-model replies cannot prove that.

## Verification

```sh
# TEST_DATABASE_URL must point to a disposable, migrated PostgreSQL 16 DB.
python -m pytest -q tests/test_mvp02a_cross_session_recall.py \
    tests/test_mvp02b_historical_user_turns.py
```

- The former MVP-02A strict XFAIL is now a required passing test: the
  **actual assembled context**, not a fabricated model answer, must contain
  the prior-session user quote, original source-turn UUID and date.
- Tests cover English-stemmed topical recall, ordinary and irrelevant input
  abstention, restricted sensitive input, correction precedence, no
  reintroduction of a Memory revision's source, tenant isolation, context
  budget, old-session provenance and idempotent restart replay.
- Full PostgreSQL and existing real Ollama workflows remain required PR
  checks; MVP-02 E2E target-model acceptance is a separate human review.
- Run the existing scenario after merge approval using:

```sh
bash scripts/run_persona_e2e.sh \
    --scenario scenarios/mvp02_memory_continuity.json \
    --report artifacts/mvp02b1-review.zip
```

**No tool, permission, memory-authority, background scheduler or service
change. No merge without explicit user approval.**
