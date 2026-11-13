# P4-06 — Policy-compliant in-app notifications and follow-ups

Architecture: Persistent Persona Agent Architecture v2.5.1 §11.2,
Phase 4 plan P4-06. Deliberately **one supported channel: authenticated
in-app inbox**. No SMS, email, desktop push, TTS, Discord or other provider
is implied; AVAILABLE means recorded for an authenticated client to fetch,
not observed/read on a device.

## Contracts and ownership

- User-global `notification_policies` start opt-out (no row means disabled).
  Explicit authenticated policy command sets channel opt-in, IANA timezone,
  and half-open quiet-hour interval expressed in local minutes; changes are
  optimistic-versioned. Equal start/end means no quiet hours.
- Authenticated `schedule_notification` requires selected Foreground persona,
  generation, explicit action, a due instant with UTC offset, IANA timezone,
  purpose REMINDER/FOLLOW_UP and optional exact source conversation/turn
  provenance. The action is NOT a model-generated permission. Recurrence
  supports NONE and DAILY only. Other channels and automatically inferred
  goal suggestions require subsequent authorization/product policy decisions.
- Scheduling stores the originating persona, not only the current selection.
  Platform `dispatch_due_notifications` creates immutable logical
  occurrences and an event/outbox atomically without persona inference,
  Goal/Task/Procedure mutations, or lifecycle changes. This is a deterministic
  communication-domain poll, NOT an additional autonomous P0 tool scheduler.
- One-shot reminders are only offered once. Daily reminders use the intended
  local wall clock and choose the first occurrence in ambiguous fall-back
  times; nonexistent spring-forward wall times roll forward to the next
  available time. Missed occurrences >24h old are skipped with a durable
  event, and DAILY advances directly to the next local day (no backlog spam).
  Quiet hours defer due occurrences until delivery becomes permitted.
- `list_notification_inbox` exposes AVAILABLE events from **any** owned
  persona, including inactive ones. Neither reading nor publishing inbox
  changes current persona/Goal state. Delivery/read receipts are distinct:
  AVAILABLE does not imply the user read a message.
- Authenticated `respond_to_notification` supports ACK, STOP, SNOOZE (bounded
  minutes), and REPLY; exact idempotency receipts and event/outbox. STOP
  cancels future occurrences **and suppresses already snoozed pending reminders**. REPLY is a durable, persona-scoped
  `PENDING_PERSONA` mailbox item, **not** a conversation turn or cognitive
  inference. It can be inspected only when the originating persona is
  explicitly selected in Foreground via
  `list_pending_notification_replies`. Consumption into Conversation
  is outside this first transport slice and must never auto-reactivate
  a persona.

## Runtime

Deploy the deterministic in-app notifier alongside existing workers using
`python -m persona_agent.notification_worker --dsn "$DATABASE_URL"`.
`--once` allows an idempotent cron/host tick. This worker polls only
PostgreSQL due intents; no persona, cognition or external transport process
is invoked. The authenticated frontend consumes the inbox API separately.

## Failure behavior

- Existing PostgreSQL transactions, stream events and transactional outbox
  preserve intent, occurrence, policy and action state. Duplicate scheduler
  ticks use row locks and unique per-intent scheduled occurrence keys;
  duplicate user commands replay only when exact command payload matches.
- No network/provider call exists. The only possible delivery effect is a
  single durable in-app occurrence. No "exactly once" promise for future push
  channels. Provider ID, idempotency, accepted/sent/read states and retries
  must be separately specified before adding a push transport.
- Quiet-hour enforcement and global opt-out are checked at actual due scan,
  including after policy changes. A disabled user has no due deliveries.
- Source-user and origin-persona are durable; callback correlation uses
  occurrence ID, authenticated user scope, and a command idempotency key.
- A local calendar shift or DST does not execute any persona cognition.
  Timezone version changes apply to future schedules only; user-global
  policy timezone controls quiet hours. Simple daily cadence is intentional.
- A specific numeric cap for *auto-derived* follow-ups remains undecided in
  v2.5.1, so this slice permits only explicitly user-scheduled follow-ups.
  It does not grant Goal/Cognitive subsystems autonomous outbound authority.

## Verification

Real PostgreSQL tests cover opt-in, inactive persona inbox and queued reply,
idempotency/event-outbox, quiet hours, daily and one-shot missed schedules,
DST gap/fold, snooze/stop, user isolation, and concurrent due polls.
Existing P0/Phase 4 and live integration suites must stay green.
