# P2-02 — Incremental Summaries & Episodic Retrieval

Authority: Persistent Persona Agent Architecture v2.5, Sections 5, 9, 14,
19–20 and acceptance C02–C05/C14. This is a bounded Phase 2 vertical slice.

## Contracts and ownership

Conversation turns/responses remain authoritative, immutable source evidence.
The Memory System owns derived summaries and episodes; Conversation owns the
progress watermark (`conversations.summary_watermark`) and recent dialogue.
The two are committed atomically. An episode is **not** a summary and a
summary is **not** a replacement for a user instruction, verified fact,
personal semantic meaning, reflection, goal, or procedure execution state.

- `conversation_summaries`: ordered, non-overlapping contiguous
  `[start_sequence,end_sequence]` with first/last turn IDs, source digest,
  model ID and prose. These are brief factual accounts of discussion,
  not agent inner thoughts.
- `memory_episodes`: optional individual significant experiences, topic,
  timestamp, source-turn quote and confidence. These are labeled
  `MODEL_INTERPRETED` and must not automatically become verified user facts.
- `conversation_summary_jobs`: durable source range correlation to existing
  P0 `operations`, with selection-generation-specific retries. A crash
  after staging but before enqueue is repaired by idempotent rescheduling.
- `SummaryDraft` / `EpisodeDraft`: validated model proposals. Every episode
  must cite a user-turn UUID inside the summarized range and quote an exact
  case-insensitive span of that authenticated user input.

All writes remain User × Persona scoped. Source turn boundaries, original
messages, delivery status, and episode evidence can be re-read after restart.

## Execution flow

1. A trusted producer calls `schedule_summary` after conversation delivery
   (or at a session boundary). It leaves `retain_recent` verbatim turns,
   caps each range (default: 8 turns) and refuses unfinished or undelivered
   inputs. No summary is needed for a short conversation.
2. The producer stages a source-range job then calls the existing P0
   `enqueue_operation` with `FOREGROUND_ASYNC/P1`, capability
   `memory.summarization` and pool `cognition.llm`. These must be configured
   through the normal trusted deployment resource pool configuration.
3. The P0 scheduler grants a fenced lease. `prepare_summary` snapshots
   transcript/delivered output; `run_summary` invokes the configured
   `TextModelGateway` **outside** the PostgreSQL transaction.
4. `commit_summary` revalidates operation lease, persona authority,
   watermark, source digest and every episode quote in a short transaction.
   It appends one versioned summary, 0–3 optional episodes, the watermark,
   an event and matching outbox row.
5. `complete_operation` records the durable summary ID. If a worker crashes
   between summary commit and operation completion, the next valid lease
   completes without asking the model again. Concurrent jobs cannot advance
   the same watermark twice.

This slice provides the durable schedule/worker API, not an automatic
always-running extractor or background evaluation barrier. The runtime
operator or a later event consumer must invoke the trusted scheduling entrypoint.
It does not introduce autonomous persona goals.

## Retrieval and context

- `search_episodes` searches topic/description plus explicit time bounds
  within a single User × Persona; `episode_source_turn` recovers original
  evidence, including delivered assistant output only.
- For a long ongoing conversation, `prepare_text_context` optionally
  includes up to three previously compacted ranges older than the selected
  recent-turn window. Current exact user input and recent messages remain
  primary. The historical block is labeled approximate and untrusted.
- For a historical recall cue, the Context Builder may retrieve a bounded
  relevant cross-session episode. Unrelated inputs do not scan episode
  history. Ambiguous date phrases are not silently guessed into date filters;
  typed retrieval callers can pass precise UTC-aware bounds.
- PostgreSQL full-text search is the initial derived index; no embeddings,
  external DB, Redis, or additional LLM call on the interactive path.

## Safety and limitations

The model's summary text is an unverified interpretation, not ground truth.
Exact user-turn citations help with provenance but do not prove semantic
accuracy. Corrections and privacy-retention propagation for derived
summaries/episodes, automatic admission of semantic/factual memories,
sophisticated topic/date interpretation, async cognitive reflection and
automatic summary scheduling remain separate increments. Historical
transcripts remain intact and recoverable.

The conservative policy avoids summarizing pending or undelivered assistant
outputs. A late worker cannot commit if its operation fence is revoked, its
source changes, or another worker advances the same conversation watermark.
A persona switch invalidates old-generation attempts; a new job can be
staged under the new selection generation.

## Acceptance

`tests/test_phase2_summaries.py` runs against a real PostgreSQL database
and covers source-linked range compression, topic/date episodic retrieval,
cross-session context and original-turn recovery, contiguous watermarks,
idempotent scheduling, crash-after-commit replay, model citation rejection,
persona switch during inference, scope isolation, independent conversations
and tight context budgets.

The pre-existing P0/P1/P2-01 tests and live Ollama/transport smoke test must
remain green. The live smoke test is **not** a benchmark of this new model
summarization protocol; use a separately evaluated summarizer model before
deploying unattended background summarization.


## Review hardening — byte-bounded source projection and fair restore

An accepted text turn can contain 12,000 Unicode characters (or a similarly
long delivered assistant message). Its original UTF-8 representation may
exceed the 32,000-byte model-source budget even when the summary range has
only one turn. The **authoritative source digest always covers the entire**
original input, delivered assistant output and anchored follow-ups; it is
never truncated. The summarizer sees a deterministic, UTF-8-byte-bounded
head/tail excerpt of each turn, with an explicit omission marker and a cap
on visible follow-ups per turn. All turns in the selected range remain
represented, even when a single source turn is oversized. The summarizer is
instructed not to make claims about omitted material. A source-size failure
can therefore no longer permanently prevent a contiguous watermark from
advancing. This is a bounded *lossy* view for summarization, not evidence
that omitted content was read or a replacement for immutable transcript
retrieval.

When restoring up to three older summaries, the Context Builder allocates
its limited summary budget fairly among eligible ranges, retaining each
range's turn numbers and marking omitted middle text. An accepted summary
may be as long as 4,000 characters; it no longer suppresses all previous
summaries when it exceeds the 1,100-character context-block cap. Budget
sharing and head/tail excerpts preserve the latest user input and recent
verbatim turns; retrieving original turns remains the authoritative way
to inspect omitted details.

PostgreSQL regression tests cover a 12,000-character multi-byte input plus
a 12,000-character delivered response (over 70 KB of original evidence),
subsequent watermark progress, and restoration of multiple 1,200+ character
summaries in a bounded response context.
