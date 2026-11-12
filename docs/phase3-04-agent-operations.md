# P3-04 — Authorized AGENT Execution Integration (registered read-only vertical slice)

**Authority:** Architecture v2.5.1 §§8.1–8.2, 9.1–9.4, 11.3, 21.4; merged P3-02 completion corrections and P3-03/P3-04 procedure contracts.

## Security and ownership

This increment permits **only** the existing registered read-only `text.statistics.v1` capability, wrapped as `procedure.agent.text.statistics.v1`. It is a limited executable vertical slice, **not** a generic AGENT tool runner, shell executor, browser access, or permission to perform external side effects.

- **Task domain** owns AGENT task status, capability, deadline, suspension, priority and effective transitive prerequisites. A run may link to an AGENT task only when AGENT step capabilities match that task; a USER task cannot launder an AGENT step into the scheduler.
- **Procedure Manager** owns the immutable method/step pin and an explicit `AuthorizeAgentStepCommand` recording a source turn plus a *separate exact user approval*, selected persona generation, expected Task/Run/Step versions, a precise operation ID, capability and bounded parameters. Its staging row is persisted **before** P0 enqueue and is idempotently recoverable after a crash. A second pending authorization for the same step is rejected.
- **P0 scheduler** is the **only** operation scheduler. `EnqueueOperation.task_id` remains rejected for every operation type except this exact registered wrapper, with required `input_versions.task`, `input_versions.procedure_run`, and `input_versions.procedure_step`. The operation request fixes `READ_ONLY`, `USER` origin, `FOREGROUND_ASYNC`, `P1`, the registered `tool.readonly` resource, `text.statistics.v1` capability, bounded timeout/retry budget, and deterministic correlation. Unstaged, cross-user, altered or expired requests cannot enqueue.
- **Worker** calls the existing P0 `claim_operation` with an explicit operation-type filter; a generic worker cannot accidentally claim this new type. It validates currently selected persona, task policy, run/step and exact versions **at enqueue, claim, immediately before invocation and operation completion**. It performs the existing registered deterministic read-only text-analysis implementation **outside database transactions**. P0 owns claim-generation fencing, leases, resources, retries and cancellation.
- **Procedure result reconciliation** is a trusted platform-only, idempotent, domain-owned process. It consumes only a terminal P0 operation. `COMPLETED` is accepted as a `VERIFIED` *registered operation result* only when `effect_status='CONFIRMED'`, the exact result reference and `claim_generation` match a durable read-only result row, and task/run/step authority has not changed. The step event is appended with `TRUSTED_PLATFORM` provenance, and the run/step projections version together with the outbox. Failed/cancelled/unknown outcomes remain distinguished. A stale or revoked result is `INVALIDATED` without altering the procedure's steps.
- **Goal/Task domain remains authoritative:** accepting a verified tool result is **not** automatically task completion, proof of the overall method's success criterion or evidence of goal attainment. A completed mixed run requires independently user-confirmed USER steps and trusted operation-backed VERIFIED AGENT steps.

## Cancellation and recovery

The existing P0 `cancel_operation` remains the explicit cancellation API. A task suspension, deadline expiry, prerequisite correction, run cancellation, step correction or persona switch prevents **new claims**; version fencing also rejects stale P0 completion. In-progress read-only tool computation may return, but its result can never be committed as a verified step after revocation. No attempt is made to reverse already executed code.

A crash between authorization staging and enqueue is recovered by replaying `schedule_agent_step` with its original approved command. A crash after P0 completion but before Procedure reconciliation is recovered by repeating `reconcile_agent_step`. Stale terminal results are recorded as invalidated evidence rather than modifying newer progress. `procedure_agent_operation_results` is append-only; orphan attempt results are ignored unless P0 claims matching completion.

Authorizations are limited to one pending operation per step; P0 retries reuse that operation under claim-generation fencing. A corrected AGENT step may be retried by explicitly creating a new run. This PR does **not** silently reset an old successful operation or introduce automatic retry-after-correction semantics.

## Intentional limits / later work

- The gateway has no supported general-purpose browser, repository editor, deployment tool, filesystem executor, or external-effect provider. Each future capability requires a distinct typed request schema, scope/permission decision, provider idempotency, effect reconciliation and cancellation behavior. Do **not** broaden this restricted whitelist by accepting a string capability name from a model.
- Only read-only registered `text.statistics.v1` is executable here. That validates the P0/Task/Procedure integration contract; it does **not** complete the full general AGENT execution roadmap.
- Conversation acknowledgments, user-reported procedure progress, and Task completion remain separate states. P3-05 still owns method effectiveness and safe adaptation.

## Acceptance

`tests/test_phase3_agent_operations.py` validates verified operation provenance and Task noncompletion; explicit approval and generic-task-enqueue denial; unsupported capability rejection; task version revocation before claim; P0 cancellation during worker processing; stale result invalidation after run state changes; idempotent stage and result replay. Full PostgreSQL, P0 contract and real Ollama CI must pass before review and merge.
