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


## Review hardening — terminal retry, post-lock fencing, episode recall

- **Fresh admission after terminal failure.** `schedule_summary(..., retry_failed=True)`
  is an intentional, authenticated retry boundary. The previous operation's
  `FAILED` state, command receipt, and source job history stay unchanged.
  A new monotonically numbered `admission_attempt` uses a different
  operation UUID and idempotency key for the same contiguous watermark range.
  Repeated scheduling of queued, running, retry-waiting or staged-but-not-
  enqueued work always replays its existing operation. Without explicit
  `retry_failed=True`, a failed job raises `SummaryNotReady` instead of
  silently consuming unlimited model attempts. Cancelled, blocked, and
  suspended jobs are not automatically restarted.
- **Lease check after potentially blocking locks.** `commit_summary()`
  repeats the P0 `require_claim()` fence after acquiring the conversation
  `FOR UPDATE` lock, and again after inserts/outbox append but before
  returning from the transaction. If it expires while blocked or during
  validation, the summary, episodes, watermark and event/outbox writes roll
  back together.
- **Budgeted episodic restoration.** The Context Builder distributes the
  episode block's optional character budget across eligible matches,
  preserves each topic, source UUID and source-turn date, and marks truncated
  episode descriptions. One valid 1,200-character description does not
  suppress itself and all older matches.

New PostgreSQL regressions cover three failed P0 operation claims followed
by explicit new admission, a real blocked conversation lock across worker
lease expiry, and two oversized episodic descriptions recalled together.


## Review hardening — summary cutoff follows budgeted history

The SQL LIMIT window is only a *candidate* for recent verbatim history.
When character budgeting drops messages from that window, its first
sequence is not the first message actually retained. Historical summary
retrieval now uses the oldest **retained** message sequence, tracked
across user turns, delivered replies and delivered tool follow-ups.

Summary text itself consumes context and may displace additional messages.
The Context Builder therefore recomputes the candidate summary block and
the budgeted recent-history boundary until the cutoff stabilizes (bounded
by the number of candidate turns, with a monotone cutoff). It does not
reintroduce verbatim messages below the resulting boundary, avoiding
overlap with newly eligible summaries. This keeps the latest user input
prioritized without silently discarding eligible compacted history.

A PostgreSQL regression builds 20 roughly 1,000-character turns and
summarizes ranges 1–8 and 9–16. With a tight 4,000-character context
budget, both historical ranges are restored when the recent window is
trimmed. With a roomy budget, 9–16 is represented verbatim and its summary
is correctly excluded from the system prompt.


## Review hardening — permanently fenced assistant deliveries

The summary-readiness gate distinguishes a recoverable `GENERATED`
assistant response from one whose original selection generation has been
revoked. `acknowledge_text_delivery()` cannot authorize an output after
its original selection generation is revoked, even if the user later
reselects the same persona. The original user turn can therefore be
summarized under a newly authorized generation, **without** projecting
the undelivered assistant text as something the user heard.

- `GENERATED` with the **current generation** remains ineligible until
  actually `DELIVERED`; pending messages still block as before.
- `GENERATED` with a **revoked generation** no longer blocks the
  contiguous watermark. The original generated response remains stored
  with `GENERATED` status for traceability; its text is excluded from
  the canonical delivered history and model summarization input.
- `DELIVERED` responses remain included regardless of subsequent
  persona switches because delivery evidence was committed in the
  original generation.
- Scheduling and summary commit require the current selected persona's
  fenced authority, so a late acknowledgement cannot race the revoked
  classification and become a valid delivery retroactively.

PostgreSQL regression: generate but do not acknowledge a response, confirm
summarization is blocked while delivery remains authorized, switch to
another persona and back, confirm the original response can no longer be
acknowledged, then summarize the source user input without the unsent text
and advance the next contiguous watermark range.
