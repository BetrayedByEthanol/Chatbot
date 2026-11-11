# Phase 1 — Integrated Acceptance (v2.5 MVP)

## Status and scope

This slice validates the existing, merged P0/P1-01/P1-02/P1-03 contracts
together. It adds **no new domain persistence schema, authority source or
scheduler**. It is an intentionally local-only acceptance harness, not a
production authenticated web service.

Architecture v2.5, roadmap Phase 1 acceptance: small immediate tool
answers, requested slow tool yields without blocking subsequent text turns,
and restart restores durable operation/response correlation. Also exercise
the baseline model gateway, bounded recent context, selected persona,
conversation state and basic latency metrics.

The harness comprises:

- \`python -m persona_agent.acceptance_host serve\` — JSON-lines text
  transport on stdin/stdout. It trusts operator-supplied user/persona IDs
  and **must not** be exposed as a remotely accessible unauthenticated API.
  Incoming packets have explicit \`mode\` (model/time/analyze) — no
  speculative LLM tool-intent classifier is added.
- \`python -m persona_agent.acceptance_host worker\` — one P0-fenced,
  resource-aware text statistics worker. A separate \`WorkerSupervisor\`
  watches/restarts the child subprocess with bounded restarts (five starts
  per rolling minute). Worker startup/repeated polling invokes
  \`reap_stale_operations\`, permitting expired READ_ONLY leases to retry
  under the scheduler's existing caps/backoffs.
- \`LocalAcceptanceSession\` — persists input through \`ingest_text\`,
  processes model/time/analysis through the existing adapters, checks
  replay authority just before emitting, ACKs delivery only after the
  channel flushes, and retrieves READY delayed follow-ups on \`poll\`.
- \`seed\` — explicit **operator-only disposable database** fixture. It
  creates one selected Foreground persona and configures the registered
  \`tool.readonly\` pool. It is not available as a model/user JSONL action.
- \`get_conversation_metrics\` — existing PostgreSQL-derived response
  output-ready/delivered latency, backlog and tool metrics.

No new LLM inference is performed for pending operation polling. Model
output is ordinary text and never parsed as a privileged command.

## Quick start (local operator)

Have PostgreSQL, Python 3.11+, and Ollama serving a pulled model.
Use a **disposable** database for \`seed\`; apply migrations 0001–0007
first, once, in order.

\`\`\`sh
python -m pip install -e ".[test]"
export ACCEPTANCE_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/phase1demo'
# Example initial schema setup on a disposable DB:
python - <<'PY'
import os
from pathlib import Path
import psycopg
with psycopg.connect(os.environ["ACCEPTANCE_DATABASE_URL"],autocommit=True) as db:
    for path in sorted(Path("migrations").glob("*.sql")):
        with db.transaction():
            db.execute(path.read_text())
PY
python -m persona_agent.acceptance_host seed
\`\`\`

The \`seed\` command returns \`user_id\`, \`instance_id\`,
\`conversation_id\` and \`generation\`; reuse exactly those values
for the host and following restarts.

\`\`\`sh
python -m persona_agent.acceptance_host serve \
  --user-id '<user-id>' --instance-id '<instance-id>' \
  --conversation-id '<conversation-id>' --generation 1 \
  --model 'qwen2.5:0.5b'
\`\`\`

Send newline-delimited JSON on standard input (one action per line):

\`\`\`json
{"action":"message","idempotency_key":"one","mode":"model","text":"Hello!"}
{"action":"message","idempotency_key":"two","mode":"time","timezone":"UTC","text":"What time is it?"}
{"action":"message","idempotency_key":"three","mode":"analyze","max_words":3,"text":"alpha beta beta gamma"}
{"action":"poll"}
{"action":"metrics"}
{"action":"quit"}
\`\`\`

The worker runs in a supervised child process by default. Poll again
later for a slow result. \`recover\` resends saved but undelivered
GENERATED replies after a transport restart; a PENDING input with no
generated response must be retried using the **same stable message
idempotency key and text/mode**. The local channel cannot guarantee
exactly-once external output in a crash window between write and ACK;
the durable record enforces idempotent acknowledgment, not atomic output.

Stopping the transport normally stops its supervised child; queued
requested work remains durable for the next worker. Process crashes may
leave RUNNING claims until the lease expires and P0 recovery retries
them. No DB locks are held during model/provider/tool execution.

## Real model and acceptance evidence

The \`.github/workflows/phase1-acceptance.yml\` job deploys PostgreSQL 16
and the official Ollama container, pulls \`qwen2.5:0.5b\`, applies
all migrations to an ephemeral DB and runs
\`scripts/phase1_live_acceptance.py\` against a **real** non-streaming
model. This script exercises two actual inference calls, induced
transport-disconnect recovery without re-inference, the immediate
clock tool, a slow scheduled text-analysis tool, supervised worker
completion, follow-up dedupe and latency reports.

\`tests/test_phase1_integrated_acceptance.py\` separately uses a mock
Ollama HTTP endpoint to test protocol integration deterministically;
that suite is **not** proof of real model inference. The real workflow's
success logs and model ID are required before claiming the model gate
passed.

Acceptance gate expectations:
1. Existing PostgreSQL suites + integration tests pass.
2. Real Ollama invocation returns non-empty bounded text via existing
   typed adapter; conversation context survives a host restart.
3. Time tool responds immediately. Slow tool yields a short reply and
   later a delivered follow-up; worker crash/restart and expired lease
   recovery do not duplicate accepted results.
4. Response and follow-up outputs are ACKed after transport delivery,
   with durable replay/trace correlation and no cross-user leaks.
5. Record P95 time-to-output-ready and delivery rather than inventing a
   time-to-first-token. No numerical latency SLA is approved by v2.5;
   record the measured hardware/model and decide a threshold separately.

Remaining outside this local MVP gate: authenticated public transport,
actual streaming TTFC, STT/TTS, GPU-budget enforcement, autonomous
Background cognition and full Phase 2 memory/emotion/goal functionality.
Phase 1 exit **must remain conditional** until the live workflow has
succeeded on the feature branch and any resulting defects are fixed.

## Integrated-test discovery: deterministic tool ID conflict

During a full concurrent PostgreSQL run the pre-existing P1-02 first-time
admission race was found to conflict on `tool_call_id`'s PRIMARY KEY
before the narrow `ON CONFLICT(turn_id)` handler. It now uses
`ON CONFLICT DO NOTHING` and rereads the winner by the authenticated
`turn_id`. A conflicting PK from another turn raises typed
`IdempotencyConflict`; it cannot become a cross-turn replay or raw
`UniqueViolation`. The original concurrent admission regressions remain,
plus an explicit deterministic-ID collision case.

### Text transport process gate

The live model acceptance script also spawns
`python -m persona_agent.acceptance_host serve --no-worker` as a
**separate operating-system process**, writes newline-delimited JSON
requests to its stdin, parses its stdout, and verifies a real model
response, clock response and persisted delivery metrics. A deterministic
integration regression runs the same subprocess protocol against the
mock HTTP Ollama endpoint. This prevents an in-process-only acceptance
demo from being mistaken for verification of the actual text transport.

## Review hardening — page past deferred tool results

`LocalAcceptanceSession.poll_followups(limit=20)` now treats `limit` as
the maximum **delivered** follow-ups per invocation, not as a cap on
candidate records examined. It reads scoped candidate rows in small,
ordered keyset pages using immutable `(created_at, tool_call_id)`, and
continues past PENDING or DEFERRED rows until it either reaches the
delivery cap or exhausts candidates. Terminal DELIVERED/SUPPRESSED
results are excluded from subsequent polls. No schema changes, durable
cursor checkpoints or new global scheduler are required.

Every candidate still passes the existing reconciliation, preparation,
authorization and delivery-ACK fences; failed channel delivery does not
mark an output DELIVERED. A new PostgreSQL regression creates 22 analysis
turns, completes all operations, leaves 21 earlier outputs DEFERRED,
and verifies the latest result is delivered on the first poll even with
a page size of three. It also verifies no duplication after a new host
session. Additional tests check that `limit` caps outputs rather than
how many old non-ready calls are inspected.

**Tradeoff:** A poll may scan many deferred calls, one bounded DB page at
a time; this is appropriate to the small, local Phase 1 acceptance harness.
A scaled production transport should maintain an indexed candidate cursor
or a separate fair dispatch queue rather than relying on O(backlog)
scanning in a latency-critical foreground request.
