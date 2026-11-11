# P0-05 — Phase 0 Integration & Acceptance (v2.5)

## Scope and status

This increment is an **end-to-end PostgreSQL acceptance gate** on top of
P0-01 through P0-04, not a new agent worker, conversational model runtime,
or unauthorized command dispatcher. It exercises actual public service
contracts and database state in one disposable PostgreSQL integration suite.

## Phase 0 exit criteria

The v2.5 roadmap specifies switch isolation, command idempotency, event replay,
unauthorized-action denial and competing-worker claims as the Phase 0 exit gate.
P0-05 adds cross-boundary verification alongside the existing domain-specific
tests:

| Contract | Executable acceptance |
|---|---|
| Authenticated command → receipt → durable event/outbox | Rename replay and operation enqueue replay produce exactly one result |
| Event fanout and replay fencing | Outbox dispatch, consumer claim, DB-only handler/ACK, stale duplicate ACK rejection |
| Explicit authenticated operation admission | Persona input version is checked after a delivered rename; no handler bypass |
| Worker execution | Queued → claimed → checkpoint → completed, lease fencing and resource release |
| Selection authority and tenant scope | Switch invalidates prior worker; other user cannot resume; explicit switch-back/re-admission |
| Competing claims | Two concurrent worker claims cannot execute the same queued operation |
| Crash recovery | Expired internal claim is retryable with a higher fencing generation |
| External-effect safety | Dispatched expired attempt is quarantined pending provider evidence |

Run with:

\`\`\`sh
TEST_DATABASE_URL=postgresql://... pytest -q tests/test_phase0_acceptance.py
\`\`\`

The existing workflow runs this with all Phase 0 contract/integration tests.

## Transaction handoff (intentional v2.5 boundary)

\`process_delivery\` owns a database transaction and permits a **database-only**
handler. \`enqueue_operation\` is a separately authenticated user-admission
entrypoint requiring an **idle autocommit** connection. The acceptance path
consumes an event, acknowledges it, and then admits a new user-authorized
operation through its own transaction. Calling \`enqueue_operation\` inside a
delivery handler would violate its transaction and authorization boundary.

Later task/cognitive producers must define their own authorized dispatch
contracts and any atomic scheduling handoff. P0-05 does **not** claim such an
autonomous handoff exists.

## Deliberate Phase 0 limits

Passing Phase 0 tests establishes the durability and safety foundation.
It does not prove end-user text conversation, contextual memory,
emotional continuity, recursive goal execution, tool-provider reliability,
Background preparation, or asynchronous worker loops; those belong to later
roadmap phases. This slice changes no database schema or existing domain
ownership contract. No main-branch merge is part of acceptance.
