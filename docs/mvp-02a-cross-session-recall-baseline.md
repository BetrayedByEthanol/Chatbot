# MVP-02A — Cross-Session Recall Regression Baseline

**Authority:** Persistent Persona Agent Architecture v2.5.1, Memory/Conversation
boundaries. **Baseline:** merged MVP-01F.4 on `main` (`df07d363`).

This increment implements **evidence collection and a failing behavioral
acceptance contract**, not historical recall itself. PostgreSQL remains the
authoritative source of conversation turns. No new memory owner, embedding
service, schema, worker, prompt injection or tool capability is introduced.

## The reproducible gap

Ordinary user input:

> I've been thinking about walking more regularly.

After a host restart and a new conversation for the **same** user/persona:

> Do you remember what we discussed about walking?

The prior utterance is durably stored as a Conversation user turn. Existing
explicit extraction does not generally admit this non-allowlisted utterance
as a structured normal-privacy Memory record, and summary/episode workers
are not started by the MVP-01E runner. Therefore the existing model context
may have no evidence to support an accurate recollection.

This is a **context availability** acceptance requirement; forcing the
deterministic test model to repeat the walking fact would provide no evidence
that retrieval works.

## Added artifacts

- `scenarios/mvp02_memory_continuity.json`: two conversations, two host
  restarts, a direct recollection request and an outdoor-activity paraphrase
  diagnostic. Both recall questions require human review of the *real*
  model request and answer.
- `tests/test_mvp02a_cross_session_recall.py`: drives the existing
  `run_scenario()` + `BehavioralAcceptanceHarness` against migrated
  PostgreSQL and a deterministic model. Verifies source-turn durability,
  separate conversation IDs, worker checkpoint, idempotent restart replay,
  another-user isolation, and exported model-context/review evidence.
- **One strict `xfail` regression:** `test_recall_context_contains_prior_user_source_and_provenance`
  requires the original user quotation, immutable source-turn ID and source
  date in the next conversation's *system context before model inference*.
  The older source **must not** be impersonated as a current-session user
  message. On the present `main` this is an expected **XFAIL**. A future
  `XPASS(strict)` intentionally fails the CI until the marker is removed
  after proving MVP-02B's retrieval behavior.

The deterministic gateway is labeled `pre_gateway_unverified`; it is **not**
claimed to be the exact outbound HTTP prompt. An actual Ollama run uses the
existing `ollama.outbound` capture and the same scenario file.

## Verification

With an isolated migrated `TEST_DATABASE_URL`:

```sh
python -m pytest -q tests/test_mvp02a_cross_session_recall.py
```

Expected on the **unmodified retrieval path**: five green control tests and
one strict XFAIL; no unexpected failure. If no `TEST_DATABASE_URL` is
configured, the PostgreSQL tests skip, and no runtime behavior is proven.

With local Ollama, Docker and `.env.e2e` configured:

```sh
bash scripts/run_persona_e2e.sh \
  --scenario scenarios/mvp02_memory_continuity.json \
  --report artifacts/mvp02a-baseline.zip
```

The ZIP captures transcript, turn state and actual Ollama messages for human
review. A passing preexisting deterministic evaluation is **not** a claim
that memory recall passed. A missing prior-session quote is an MVP-02B
retrieval failure, even if the model happens to guess a plausible answer.

## Required follow-on implementation: MVP-02B.1

First use the existing Memory/episode evidence; when missing for an **explicit
historical recall**, retrieve a small, bounded number of authenticated
prior-conversation **user** turns, with immutable IDs and timestamps, using
PostgreSQL. Apply privacy/retention/selection constraints, preserve user- vs
model-interpreted epistemic labels, and avoid extra foreground LLM calls.
Measure context size and query latency. Never automatically admit a Goal,
Memory, procedure, or user preference from an unverified recalled quote.

After the source-anchored regression passes, remove the `xfail` marker and
validate paraphrased retrieval and faithful *actual-model* answers separately.

**Not done here:** implementation of historical retrieval, semantic embeddings
or pgvector, Qdrant/Valkey, generalized memory extraction, summarization worker
orchestration, model-level acceptance approval. Do not merge automatically.
