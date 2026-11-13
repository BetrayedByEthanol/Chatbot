# P4-05d — Bounded Background recovery

Authority: Persistent Persona Agent Architecture v2.5.1 and
`docs/phase4-plan.md`; builds on P4-05a/b/c. Changes remain in P0's existing
lease scheduler and the existing Procedure-owned read-only gate.

## Recovery matrix

| P0 state/effect | Trigger | Resolution |
| --- | --- | --- |
| RUNNING + PREPARED, same valid Background epoch | Tool exception | RETRY_WAIT with P0-owned exponential delay; retry requires a new fenced claim; third registered AGENT attempt fails terminally |
| RUNNING + PREPARED, same epoch | Lease/timeout expiry | P0 reaper releases reservations and records RETRY_WAIT (2s/4s) or FAILED when attempt budget exhausted |
| Expired P2 attempt with Task deadline elapsed | P0 reaper | FAILED; do not silently requeue a task that can no longer run |
| Live RUNNING after Foreground return | Epoch revocation | SUSPEND_REQUESTED, worker cooperative ACK, no premature resource release |
| SUSPEND_REQUESTED / revoked P2 with expired worker claim | P0 reaper | SUSPENDED with no fabricated checkpoint; resources released |
| SUSPENDED + valid immutable source checkpoint | Reaper/hygiene scan | Preserve; only fresh authenticated P4-05c approval authorizes a new operation |
| SUSPENDED + **no valid matching checkpoint** | Subsequent P0 hygiene | BLOCKED idempotently, with event/outbox; old authorization may be superseded by fresh user approval, never automatically resumed |
| Any externally DISPATCHED uncertain effect | Timeout/cancel/crash | Existing P0 OUTCOME_UNKNOWN quarantine; no replay, resources retained until operator/provider reconciliation |

For the single supported `text.statistics.v1` Background tool, the
`max_attempts=3` contract already exists in P0 admission. Its RETRY_WAIT
is now bounded by a deterministic P0 delay of
`min(60, 2 ** min(attempt_count, 6))` seconds (attempts 1 and 2: 2s, 4s).
Worker-reported failures and reaper-recovered crashes use the same policy.
Failed third attempts are terminal; no new claims can reuse them.
No extra scheduler, retry framework, migration, LLM or external provider
is introduced. Each fresh, separately approved next-epoch P4-05c operation
has its own explicit P0 attempt budget; this is *not* permission to retry a
stale previous operation.

## Checkpoint and authorization boundaries

Generic `checkpoint_operation` rejects registered GOAL/P2 AGENT attempts:
only the Procedure-owned immutable safe-stop checkpoint created atomically
in the fenced suspension ACK is restorable. The hygiene scanner verifies
the actual checkpoint tuple (operation, generation, user, instance and
pointer), not just a non-null `checkpoint_ref`.

The worker cannot commit success after expiration/revocation; late results
do not authorize Step or Goal completion. The P0 reaper never manufactures
checkpoint progress after a process crash. A BLOCKED old operation can be
replaced only through the existing explicit user approval flow, while valid
checkpointed SUSPENDED work retains its P4-05c renewal path.

P0 external-effect `OUTCOME_UNKNOWN` and operator-evidence reconciliation
are unchanged. There is no registered autonomous external-effect executor;
this milestone does not assert crash-safe replay of arbitrary side effects.

## Validation

Real PostgreSQL tests cover read-only tool retry/backoff/exhaustion,
lease-expired claims, stale worker fences, no-result replay, uncheckpointed
suspended cleanup and fresh approval, valid checkpoint preservation,
forged opaque checkpoint rejection and Task deadline expiry. Existing
P0 external-dispatch quarantine and Phase 4 integration remain part of CI.
