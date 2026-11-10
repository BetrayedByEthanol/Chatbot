# Phase 0 slice 02 — Persona selection and lifecycle

Architecture v2.5 sections 3.1–3.3 and 9 govern this change.

## Authority and ownership

A single persona_selection row selects exactly one instance per user. The
unselected instance lifecycle is derived as INACTIVE; it is not a separate
mutable column on persona_instances.

All lifecycle mutations require an idle autocommit psycopg connection. One
short transaction validates authenticated actor and ownership, claims command
idempotency, locks persona_selection FOR UPDATE, checks expected authority,
updates the selection, appends event and outbox, persists a command result,
and commits. Errors roll back the whole operation.

## Commands

- switch_persona(connection, TrustedActor, SwitchPersonaCommand):
  Requires an expected selected instance and generation and a distinct owned
  target. Selects target in Foreground, revoking the old selection generation
  and invalidating the background epoch. The formerly selected instance is
  immediately logically INACTIVE. Replaying an already committed receipt
  never mutates state or revives a stale generation.
- record_primary_activity(connection, TrustedActor, PrimaryActivityCommand):
  Accepts only authenticated primary-user activity. Refreshes presence and
  returns the selected instance immediately to Foreground if necessary.
  Preserves the selected execution generation; increments presence revision
  even if already Foreground to fence stale idle/connection callbacks.
  Leaving Background also advances its epoch.
- enter_background(connection, actor, EnterBackgroundCommand):
  EXPLICIT_DEPARTURE requires authenticated user actor. CONNECTION_CLOSED and
  IDLE_TIMEOUT require a trusted platform actor. Requires exact selected
  instance, execution generation, and presence revision. IDLE_TIMEOUT additionally
  checks 15 minutes since last primary-user activity using PostgreSQL clock.
  Transition to Background preserves generation and advances background epoch.
- get_instance_mode(connection, TrustedActor, instance_id):
  Reads FOREGROUND/BACKGROUND/INACTIVE with strict scope enforcement.

## Safety boundaries

An event, a timer or secondary-channel traffic cannot independently select
a persona. Tools cannot synthesize authenticated user presence. Downstream
executors must check selection generation (and background epoch for
background-only work) immediately before side-effects or durable writes.

Caller-controlled Pydantic values are not themselves authentication: only
authenticated ingress/platform code may construct trusted actor objects.
Actual ingress authentication and scheduling are outside this slice.

No implicit autonomous work is permitted by Background status until the
v2.5 strict preparation barrier has completed. Checkpoint/suspension,
mailbox routing, initial instance bootstrap and worker fencing are deferred.

## Migration and tests

Apply 0001_phase0_core.sql then 0002_persona_lifecycle.sql. Test CI applies
all migration files in sorted order against disposable PostgreSQL 16.

## Decisions

- P0-02-01: INACTIVE derived from selection.
- P0-02-02: Presence revision invalidates callbacks independently of
  selection generation and background epoch.
- P0-02-03: Platform idle and connection callbacks must include the observed
  presence revision and generation; no automatic persona selection.
- P0-02-04: Initial user/persona provisioning is a separate bootstrap step.
