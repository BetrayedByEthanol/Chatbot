# P1-02 — Typed Tool Gateway & Responsive Execution

## Scope and authoritative constraints

This Phase 1 increment extends P1-01 and the v2.5 Model/Tool Gateway,
operation scheduler, and conversation-orchestrator contracts. The selected
persona and authenticated user provide authority; a tool result never grants
authorization or becomes a synthetic user message. PostgreSQL remains the
source of truth for operation status and response correlation.

Implemented, deliberately narrowly:

- **Immediate tool:** clock.now.v1. ZoneInfo-based local date/time formatting,
  bounded typed arguments, deterministic injection for tests. Produces one
  normal generated assistant response via P1-01; no worker or network needed.
- **Foreground slow tool:** text.statistics.v1. A deterministic read-only text
  statistics operation backed by the existing P0 durable scheduler, resource
  pool, worker lease, retry and recovery machinery. Its computation happens
  to be quick; scheduling it as slow demonstrates the lifecycle without an
  external provider, arbitrary URLs, model-written code or new secrets.
- **Capability registry:** typed modality, side-effect classification,
  resource pool, user authorization. Only registered operations can be
  executed by the tool worker, using the scheduler's additive
  operation_types filter (the default claim behavior is preserved).
- **Async correlation:** conversation_tool_calls maps one authorized turn to
  a stable tool/operation UUID. It records a PREPARING intent first, commits,
  calls the separately authenticated enqueue contract, then finalizes QUEUED.
  Retrying after crash reuses P0's idempotency receipt. No nested calls under
  another domain transaction.
- **Result staging:** conversation_tool_results stores per-claim-generation
  validated result text. Only a COMPLETED operation with matching result_ref
  can expose that result; abandoned lease results cannot become follow-ups.
- **Bounded foreground wait:** respond_to_text_analysis waits 0..5 seconds
  (default 50 ms) for operation completion, without holding DB locks. If
  ready, it records a DIRECT response through P1-01; otherwise it generates
  a short DEFERRED acknowledgment. The committed response model_id persists
  the decision, allowing reconciliation after restart without relying on
  a volatile callback.
- **Follow-up:** reconcile_tool_followup rechecks completed result, original
  selected persona generation, last input turn and delivery of the initial
  response. It marks READY only while current and eligible. A newer input or
  an undelivered initial response keeps the result DEFERRED; persona switch
  SUPPRESSES output. A direct result also suppresses a redundant follow-up.
  prepare_tool_followup is a scoped and authority-checked read for the output
  adapter; acknowledge_tool_followup is an idempotent delivery-state commit
  with one event and outbox row. It does *not* fabricate a new user turn.

## Operational boundaries and limitations

Before tool admission, trusted deployment code must configure resource pool
"tool.readonly", e.g. configure_resource_pool(conn,"tool.readonly",
capacity_units=2). Missing pools reject admission and leave a PREPARING
correlation record, ready for idempotent retry.

Workers call run_one_text_tool with their own idle autocommit DB connection.
A tool must not execute during another transaction. The worker uses
claim_operation and complete_operation; if the operation lease becomes stale,
normal P0 fencing rejects completion, and a staged but unconfirmed result
is never published. Provider failures use P0 fail_operation/retry semantics.

DEFERRED follow-ups caused by newer conversation activity are not
automatically resurfaced; P1-03 thread/dialogue-state evaluation will decide
whether a late result should be presented. Missing initial delivery is not
treated as a successful acknowledgment. Worker execution requires a host
process loop/supervisor; P1-02 adds the callable worker contract, not a new
daemon framework or remote HTTP service.

Output delivery cannot be made atomic with PostgreSQL. The output adapter
must recheck active authority before sending, then confirm delivery. This
module ensures validation at preparation and acknowledgment boundaries;
it does not assert atomic cross-network sends.

No live research provider, browser execution, arbitrary external tool calls,
model-initiated authority, autonomous goal execution or multimodal output is
enabled by these capabilities.

## Executable tests

Run the existing PostgreSQL workflow (migrations 0001 through 0006) and
tests/test_phase1_tools.py. Tests cover:

1. Immediate clock output and no operation admission
2. Crash-retryable PREPARING intent after missing resource configuration
3. Deferred completion through separate worker and restored DB correlation
4. Direct response on completion within await budget (no duplicate follow-up)
5. Follow-up deferred until initial output is acknowledged
6. Follow-up deferred after newer unrelated input
7. Persona switch suppresses an obsolete follow-up
8. Slow worker never claims unrelated operation types
9. Worker failure never publishes unconfirmed results
10. User isolation for tool status and follow-up inspection

## Next slice: P1-03

Introduce minimal dialogue threads, pending output relevance and measured
latency/operation metrics. Hardening should add durable worker supervision,
polling/signal wakeups and production restart drills. Phase 1 acceptance
will require a live end-to-end demonstration with a real model/transport.
