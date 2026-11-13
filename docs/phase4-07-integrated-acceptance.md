# P4-07 — Integrated Phase 4 acceptance (v2.5.1)

This milestone is **cross-domain verification**, not a new scheduler, P2
authorization rule or persistent state owner. Baseline: merged P4-01–P4-06,
including the in-app only notification channel. Existing PostgreSQL CI must
continue to pass all Phase 0–4 contracts. No migration is introduced.

## Cross-domain acceptance matrix

| Drill | Invariants verified | Test |
| --- | --- | --- |
| Complete lifecycle | Foreground approved P2 → Background preparation/ALIGN-01 READY → cooperative checkpoint → renewed user approval → next Background READY → tool + P0 reconciliation; user Task remains PLANNED | `test_safe_stop_to_new_epoch_resume_plus_inactive_mode_communication` |
| Platform communication | Authenticated follow-up delivered to in-app inbox during Background, then reply correlated to original persona with no cognition, conversation turn or Goal effect; delivery/response event outbox exactly once | Same end-to-end drill |
| Interactive priority | A synchronous P2 tool deliberately held in another thread cannot block Foreground return, opt-in, follow-up scheduling or in-app dispatch; late tool result discarded, resources released | `test_active_p2_tool_never_blocks_foreground_notifications_or_commits_late` |
| Selection fencing | Persona switch while stopped P2 invalidates checkpoint restoration, yet authenticated in-app notifications remain visible; replies inaccessible to persona until reactivated | `test_inactive_persona_reply_retains_scope_but_cannot_revive_old_checkpoint` |
| Recovery isolation | A user-approved resumed read-only operation exhausts exactly three P0 claims, without implicit Goal success or loss of an independent due reminder | `test_failed_resumed_p2_exhaustion_does_not_cancel_independent_inbox` |
| Restricted external effects | Existing P0 `OUTCOME_UNKNOWN` / external reconciliation is never auto-retried; no P2 external effect capability is registered | existing `test_phase0_operations.py`, `test_phase4_autonomy.py` |
| Delivery policies | Quiet hours, DST, opt-out, missed periods, snooze/STOP and concurrent delivery source scope | existing `test_phase4_notifications.py` |

## Real provider and latency coverage

The existing `scripts/phase1_live_acceptance.py` exercises **real
Ollama** inference, its JSON-lines host, real PostgreSQL persistence,
transient transport failure and worker restart. The Phase 4 drills use
bounded deterministic inference adapters to reliably exercise the exact
authorization, concurrency and persistence contracts; *they do not claim*
to run Background summary or Cognitive inference against a real model.

The live integration workflow now runs these Phase 4 PostgreSQL drills
in their own clean, migrated database in the same CI job after the real
provider acceptance. The Phase 0 job separately runs the entire test suite
with real PostgreSQL.

Foreground interleaving latency is recorded as
`foreground_database_roundtrip_ms` for the P2 held-tool test, without
inventing an absolute production latency SLA. Correctness is the strong
assertion that Foreground database work commits **before** the deliberately
blocked Background tool is released; the model/provider latency and
GPU/VRAM budget must still be benchmarked on target deployment hardware.

## Invariants and limitations

- A notification's AVAILABLE state is a **durable in-app inbox item**, not
  a claim that a device push, TTS, read receipt, or external provider
  succeeded; no such transport exists in P4-06.
- Notification reply ingestion does not auto-activate a persona, update
  Task/Goal outcomes, or append a new Conversation turn.
- Valid P2 authority requires an independent, explicit user approval and a
  new READY Background epoch; the old checkpoint is data only. No generic
  autonomous external effects or unattended reauthorization.
- Tests run against a **shared PostgreSQL DB** within one CI test session,
  so the established P2 cleanup fixture cancels older runnable AGENT
  operations using the authenticated API. The new tests reuse that fixture
  indirectly via `_admitted`; they never clear other users' persistent
  Goal/Procedure history.
- Existing specific-domain tests still cover event/outbox idempotency,
  operation leases, exception/recovery, P0/P1 resource reservations, thread
  races, owner-version drift, and knowledge/summary freshness.

## Exit criteria

1. Full Phase 0–4 PostgreSQL suite and real Ollama provider acceptance pass.
2. Cross-domain drills (four) pass under real PostgreSQL; no extra inference
   service or database migrations needed.
3. Independent Codex review has no outstanding findings on the PR head.
4. No merge without explicit user approval.
