# Phase 0 slice 01: persona runtime command / event / outbox

Status: first executable contract, not Phase 0 completion.

## Domain ownership and authority

The persona runtime owns instance label metadata. Users and global persona
definitions are independent; instance records are private to (user_id, persona_id).
One persona_selection row per user selects exactly one instance. Other instances
are logically inactive. Foreground / Background / Inactive transitions, with
background epochs, require the next runtime increment.

TrustedActor must originate from authenticated ingress. Raw source payloads do
not choose their own user, trust level or permission scope. The SQL foreign keys
enforce that selected instances and events stay inside the user's ownership.

## Command API

rename_instance(connection, TrustedActor, RenameInstanceCommand)
  -> RenameInstanceResult

The command supplies instance_id, expected_generation, expected_version,
new_label and scoped idempotency_key. The actor supplies authenticated user_id
and source_id. A dedicated **idle autocommit** psycopg connection is required.
An autocommit connection already inside an outer transaction is rejected:
otherwise psycopg creates a nested savepoint and this function could return
before its changes are durably committed.

The command changes only persona_instances.label, never the shared persona
definition. The owning domain validates label length; SQL prevents invalid
persisted lengths as defense in depth.

## Transaction invariants

One READ COMMITTED transaction performs the following:

1. Verify authenticated user owns the instance.
2. Insert the scoped idempotency receipt or return its prior durable result.
   Reject reuse of a key with different command parameters.
3. Lock the current selection row FOR SHARE, verify Foreground authority and
   exact execution generation. A switch must lock the same row FOR UPDATE.
4. Update the instance only when its expected version matches, bumping version.
5. Allocate a sequence in a per-instance, per-domain event stream.
6. Append the immutable event and matching outbox record.
7. Persist the idempotent result and commit everything atomically.

Errors rollback the receipt, state update, event and outbox together. No locks
are held across model inference, tools, or external I/O. Independent domains use
distinct streams rather than serializing every event for an instance.

Idempotent retries can return the committed result after a selection change,
but never perform a new mutation under an obsolete generation. This is a
deliberate distinction between replaying a receipt and granting authority.

## Run and tests

Install with: python -m pip install -e ".[test]"

Apply migrations/0001_phase0_core.sql to an empty PostgreSQL database. The
integration test suite applies this migration in its session fixture.
Set TEST_DATABASE_URL to a dedicated test PostgreSQL database and run:
python -m pytest -q

Tests cover typed commands, idempotency/collision, rollback on version conflict,
user isolation, stale generation, stream replay, parallel writes to one entity,
and parallel writes to independent users. A CI PostgreSQL service runs the suite.

## Explicitly deferred

- Authenticated HTTP/CLI ingress and owner-approved persona selection command.
- Event outbox dispatch, subscriber delivery/ack/retry and consumer deduplication.
- Operations, worker leases/fencing/resource reservation and external effects.
- Background epoch enforcement, cancellation and suspension.
- Domain contracts for conversation, memory, goals, tasks and cognition.
- Historical legacy workflow migration and the Ollama adapter.

No legacy modules or entry points are modified. The next Phase 0 slice is
selection transition + durable outbox delivery, followed by worker claims.
