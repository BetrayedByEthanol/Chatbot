# P2-05 — Retrieval & Topic Attention

Architecture authority: Persistent Persona Agent Architecture v2.5 §§18.1–18.2, 19.1–19.4, 23.1, plus the P1-03 dialogue and P2-01/P2-02/P2-04 memory contracts. Companion refinement R05 (experience-centered recall) is a candidate acceptance criterion; it does not create new authoritative state.

## Scope and domain ownership

**No new database or authoritative topic store.** Conversation already persists evidence-linked dialogue threads, `FOCUSED`, `RECENT`, `DORMANT` attention, referents, and independent `OPEN`, `RESOLVED`, `ABANDONED` resolution. P2-05 reads those existing records. Topic recency decay is **a non-persistent view** over `updated_at` and never modifies conversation resolution, a goal, task or memory retention. An unresolved but dormant conversation topic is still retrievable.

Memory remains authoritative for user-reported facts, preferences and personal meanings. P2-02 remains authoritative for source-linked **MODEL_INTERPRETED** episodes. Summaries remain available through the established, bounded *same-conversation* compression path. No model interpretation is relabeled as a verified fact and no inferred emotion or subjective perspective is promoted into memory.

## Retrieval routes

- **Mandatory context**, including persona and instructions, recent verbatim user turns and actually **DELIVERED** assistant responses, is preserved by `prepare_text_context()`. P2-02's incremental summaries and already-working explicit historical recall remain untouched.
- **Explicit historical request:** `search_relevant_history()` performs bounded indexed retrieval across `memory_records` and `memory_episodes`. Callers can constrain by **timezone-aware absolute timestamps** (`since`/`until`); no ambiguous inferred local date window. Each hit reports source class, conversation/turn, source timestamp, confidence, epistemic status and version for mutable user memory.
- **Clear anaphora to focused dialogue:** `topic_recall_block()` consults the selected conversation's stored focused thread/title and up to three topic referents. Only a narrow, deterministic short reference (e.g. “What about that?”, “Tell me more”, “continue where we left off”) triggers cross-session retrieval. A clear topic switch, ordinary chat, no focused thread or insufficient context budget performs **no additional historical query**. This costs no LLM or classifier pass and is inserted only after mandatory/recent context under the existing prompt budget.
- **Independent topic attention:** `load_topic_attention()` returns the persisted thread attention/resolution plus a derived effective status. Old `RECENT` topics can be *treated* as `DORMANT` for relevance; their stored status and ability to be explicitly reopened are unchanged.

The P2-01 user-memory and P2-02 episode cue-based context paths remain in place because they preserve already-tested long-episode abbreviation, summary cutoff, dated source identifiers and transparent epistemic labels. This slice supplies an additional on-demand hybrid API and a missing **focused-topic anaphora route**, without doubling all foreground searches on every explicit recall. A later benchmark can compare replacing the separate cue-based paths with the hybrid ranker.

## Scope, privacy, score and latency contracts

All indexed SQL lookups are confined to authenticated `(user_id,instance_id)`. User memories must be `ACTIVE`, `NORMAL` privacy and unexpired. Episodes must be `NORMAL` privacy. There is no unrestricted transcript search or arbitrary older summary scan in the foreground: original conversation turns remain accessible through the existing source-specific evidence APIs.

Index-friendly PostgreSQL full-text matches and optional temporal filters constrain candidates before ranking. A small deterministic score combines lexical match, evidence class, source recency and recorded confidence. It is an **order heuristic**, not an assertion of factual certainty. The result cap is at most ten items, with per-source limits and a character budget that abbreviates large excerpts without discarding their source anchors. Source confidence and epistemic category remain distinct. Unsupported semantic/vector similarity is not claimed; vector indexes would be derived secondary data in a future acceptance-tested slice.

No new model call, remote service, transaction-held inference, write on retrieval, or foreground worker scheduling is introduced. This implementation uses bounded database reads in the existing context-assembly transaction and does not duplicate recent verbatim history.

## Acceptance

`tests/test_phase2_retrieval.py` covers indexed user-memory and model-episode recall with source anchors; scope isolation; retraction, expiry and privacy filtering; absolute time bounds; deterministic budget and ranking; persistent FOCUSED/RECENT/DORMANT state with independent resolution and read-only recency decay; reference-based continuity; topic switches; and a negative test proving ordinary messages never call the historical candidate query.

No new migration is needed: existing P1-03 dialogue indexes, P2-01 memory GIN index, and P2-02 episode topic index are reused. Keep migrations through 0011 applied.

## Deferred

Fuzzy semantic/entity linking, conversational classifier-based topic detection, read-access feedback metrics, true vector similarity and index refresh, cross-store semantic merging, subjective experience revision tracking (R05 beyond evidence-linked episode recall), and full attention-ranked goal/cognition scheduling. These require separate evaluation and privacy contracts rather than implicit behavior changes.
