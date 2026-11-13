# P4-03 — ALIGN-01 and guarded Background readiness

**Authority:** Persistent Persona Agent Architecture v2.5.1 §§9–10, 20.4, 22.4; the behavioral implementation refinements for ALIGN-01; and `docs/phase4-plan.md`. This is a change-focused deterministic, *domain-owned* validation stage, **not** blanket LLM approval.

## Contract and boundary

On the selected Background epoch, following completed, source-watermarked `SUMMARIZE → EVALUATE → CRITICAL_PENDING`, the coordinator advances `ALIGNMENT_PENDING → READY`. It holds the selection `FOR SHARE` and the epoch `FOR UPDATE` for one short PostgreSQL transaction per tick. ALIGN-01 checks up to **32 tasks** per step and persists an immutable per-epoch task decision. Neither inference nor external tools run inside these transactions.

The Task/Goal/Procedure reader in `alignment_owner_checks.py` validates real owner records: current task status/version; Goal ancestry, active ancestors and authenticated creation provenance; direct and transitive prerequisite confirmation; user priority suspension, exclusive selection and deadline; user-approved procedure run, persona/template scope and exact run version; applicable personalized procedure revision and selected strategy; and capability-compatible AGENT steps. A child goal without a persisted approved contribution to its parent receives `REVIEW`, not an invented semantic affirmation. An AGENT task lacking a confirmed run receives `REVIEW`. Invalidated authority receives `BLOCKED`.

The Evaluation compositor (`background_alignment.py`) records each task's **ELIGIBLE / BLOCKED / REVIEW** decision and a reason, versioned Goal ancestry and ProcedureRun pins, and a priority class. `REVIEW` and `BLOCKED` apply only to affected tasks; an independent approved task can be `ELIGIBLE`. Cognition output is never a domain command or persona-role mutation. A run with fixed/pinned template version is valid when the approved version and persona scope still exist; a changed personalized variant requires review.

Before inserting any certificate, the coordinator rechecks the frozen conversation count, exact summary/cognition/critical watermarks, absence of unresolved critical updates, the count of scanned task records, and every captured Task, Goal and ProcedureRun version. A mismatch fails closed (`BLOCKED`) without a READY row. The stage event, certificate and `background_ready_epochs` record commit **atomically**. Repeated ticks are idempotent.

## Actual P0 gate

`operation_scheduler._eligible_for_policy` now requires BOTH the P4-03 certificate and a per-task `ELIGIBLE` decision for the current epoch and generation. A P2 operation without a scoped `task_id`, or without matching `input_versions.task`, cannot claim even with a READY row. It rechecks current task version/status, Goal versions and active ancestry, and ProcedureRun version/status. The P0 scheduler still owns claims, leases, resources and priority; this increment adds no second scheduler.

**P4-04 still owns new GOAL-origin P2 admission and exact step authorization.** An `ELIGIBLE` alignment finding is not permission to bypass the existing read-only tool capability and user approval requirements.

## Limitations / future work

- v2.5.1 requires *consequential* belief, role and instruction alignment. The current Persona definition has no separately versioned PERS-ALIGN-01 behavioral contract or approved parent-contribution field. A child Goal is therefore `REVIEW`, and the new stage does **not** claim a semantic validation occurred based solely on prose. No cognitive hypothesis or unverified belief can modify Persona identity or be treated as verified.
- The P4-02b instruction recognizer is conservative and its unresolved critical instructions remain a hard gate. This change **does not** implement a general authoritative permission/instruction owner or broad natural-language interpretation; a separate owner contract is needed before action-critical instructions it cannot recognize can safely enable autonomous work.
- The `priority_rank` is a validated snapshot for P4-04 to use, **not** a scheduler override. P4-04 must revalidate task/strategy/step authority when creating each operation, and should add targeted semantic review for ambiguous high-consequence actions.
- No automatic role-definition updates, strategy substitution, goal rewrites, or blanket periodic full-state LLM audits.

## Tests and verification

PostgreSQL acceptance covers an empty safe epoch (certificate but no generic P2 authority); one approved root AGENT task with a supported run alongside an independent task that needs review; return after a partial alignment scan; stale owner version between ticks; and unresolved instruction blocking. Existing P0–P3 and Phase 4 suites and the actual Ollama/PostgreSQL workflow run in CI. Independent Codex review required; **do not merge without explicit user approval**.
