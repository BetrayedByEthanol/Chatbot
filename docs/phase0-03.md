# P0-03 — durable event delivery contract

This slice implements Architecture v2.5 section 7.2 and the event-delivery
foundation for section 9. It is stacked on P0-02 and does **not** add external
tool execution, a general operation scheduler, or persona-owned cognition.

## Tables and ownership

- event_consumers and event_subscriptions register exact event-type subscribers.
  Registrations are trusted internal service operations; untrusted payloads,
  web pages or tools never register their own consumer.
- outbox remains atomic with the domain state and event journal from P0-01.
- event_deliveries has exactly one row per (event_id, consumer_id). Each row
  independently tracks PENDING, CLAIMED, PROCESSED, RETRY_PENDING, DEFERRED,
  or FAILED, with claim_generation, worker_id, lease_until, attempt_count,
  max_attempts, eligible_at and last_error.
- Subscriber registration applies to events dispatched *after* registration.
  For already-dispatched events, backfill_subscription inserts only missing
  deliveries with explicit instance authorization. Existing PROCESSED rows are
  never reset. Pause consumers before historical backfill if stream order matters.

## Core API (src/persona_agent/event_delivery.py)

- subscribe(conn, consumer_id, event_type): trusted registration.
- dispatch_outbox(conn, limit): transactional fanout of undispatched outbox
  rows to every registered subscriber. Dispatchers use PostgreSQL SKIP LOCKED
  and cannot skip an undispatched older event in the same event stream.
  A zero-subscriber event is valid and may be marked dispatched.
- claim_delivery(conn, consumer_id, worker_id, lease_seconds): atomically
  acquire one runnable delivery, incrementing claim_generation. Only one worker
  may claim an attempt. A consumer does not overtake an unprocessed older
  delivery in the same causal stream; unrelated streams and subscribers can run
  independently. CLAIMED deliveries with expired leases can be reclaimed
  while attempt_count is below max_attempts.
- process_delivery(conn, lease, handler): short, atomic transaction that locks
  and validates the current claim, invokes a DB-ONLY handler(cursor, event),
  and marks delivery PROCESSED. A stale lease, handler error, or expiry before
  acknowledgment rolls back the handler's SQL changes AND its acknowledgment.
  The journal's source_id/trust_level are authenticated provenance; payloads
  may still contain untrusted content and do not grant action authority.
- retry_delivery(conn, lease, error, delay_seconds): records failure and bounded
  cooldown or terminal FAILED. defer_delivery and resume_deferred support
  explicit gating (e.g. inactive persona mailbox). No event alone activates a
  persona.
- reap_expired_claims(conn): releases expired claims, leaving FAILED on
  exhausted attempts. Lease expiry is **not** evidence that external effects
  failed. This slice only processes DB-only handlers.
- backfill_subscription(conn, TrustedActor, instance_id, consumer_id,
  event_type): scoped, idempotent recovery for missing delivery rows.

## Transaction and recovery invariants

All mutation entrypoints require idle autocommit connections so a successful
return means a committed transaction, not a nested savepoint. The outbox and
delivery fanout commit together. DB-only consumer effects, newly created
events/outbox records (if any), and ACK commit together. Failed transaction
attempts do not mark events processed.

Durable asynchronous jobs must first be inserted as an operation in the same
transaction as the consumer ACK. Model inference/external tool use occurs
outside the transaction and is **not** attempted in P0-03. P0-04 will add
operation admission, leases, side-effect reconciliation and cancellation.

LISTEN/NOTIFY is unnecessary for correctness; workers poll durable tables.
At-least-once is the delivery guarantee; exactly-once external effects are
not claimed. A FAILED or DEFERRED earlier event blocks later same-consumer,
same-stream deliveries until explicit review. Independent consumers are not
blocked by a peer's failure.

No automatic subscription for inactive persona cognition is wired yet.
Caller must defer persona-owned cognition when instance authority is absent;
the mailbox and Background critical barrier are later implementation slices.

## Tests

Integration tests use PostgreSQL 16 and ordered migrations. They verify
fanout/no-subscriber cases, duplicate dispatch, competing dispatchers/claims,
stream causality, independent consumers, atomic DB-only processing,
rollback/retry, stale worker fencing, deferral, exhaustion/recovery,
authenticated scoped backfill, and transaction boundary enforcement.
