# P3-06 — Integrated Phase 3 Acceptance

**Architecture authority:** Persistent Persona Agent Architecture v2.5.1, especially §§18.2–18.3, 21–23. This milestone is a cross-domain *acceptance and hardening* gate, not a new runtime coordinator, planner, scheduler, or automatic model-controlled permission path.

## Acceptance scope and evidence

| Requirement | Owning domain | Executable evidence |
|---|---|---|
| Approved outcome, selected strategy and goal ancestry | Goal | `test_phase3_goals.py`; integrated user flow's approved TLS goal and selected approach |
| Task actionability, prerequisites and correction lineage | Task | `test_phase3_tasks.py`; integrated dependency correction revokes queued AGENT authority; procedure success does not complete a task |
| Shared template, pinned personal revision, STABLE/PARAMETERIZED/ADAPTIVE policies | Procedure | `test_phase3_procedures.py`; integrated chosen strategy, private method, adapted revision and pinned old run |
| Durable run, exact step evidence, cross-conversation dialogue progress | Procedure + Conversation | `test_phase3_runs.py`; integrated run linked to threads across two conversations; fresh DB reads recover progress |
| Explicit AGENT approval, Task × Run × Step admission, read-only worker, cancellation, provenance | P0 Operations + Task + Procedure | `test_phase3_agent_operations.py`; integrated prerequisite-correction invalidation, new authorization and trusted result |
| User-reported outcome vs independently verified execution and goal success | Procedure + Goal | `test_phase3_learning.py`; integrated success criterion reports, corrected evaluation and unchanged goal/task owners |
| Approved personal adaptation, stale proposal handling, historical run preservation | Procedure | `test_phase3_learning.py`; integrated APPLY after reassessment and blocked APPLY after step correction |
| User/persona isolation, selection-generation fencing and restart recovery | Persona Runtime + all owners | `test_phase0_lifecycle.py`, Phase 3 domain tests, integrated persona-switch/recovery test |
| Idempotency, durable events and transactional outbox | P0 + all owners | `test_phase0_event_delivery.py`, Phase 3 tests; integrated cross-domain event/receipt/outbox uniqueness |
| Version fencing, concurrency and recovery | Task + Procedure + Operations | `test_phase3_tasks.py`, `test_phase3_runs.py`, `test_phase3_agent_operations.py`, `test_phase3_learning.py`; integrated stale authority and correction scenarios |

## End-to-end scenarios

`tests/test_phase3_integrated_acceptance.py` runs four intentionally cross-domain scenarios against a real PostgreSQL database, with a new DB connection for every domain command.

1. **Approved user workflow:** approve goal → select strategy → publish reviewed PARAMETERIZED template → create goal/strategy-bound private revision → create dependent tasks → start task-bound run → link dialogue threads in different conversations → record and confirm two USER steps → finish run → user-report the template's success criterion. Confirm that neither procedure success nor method assessment completes a Task. Only an explicit Task report and confirmation unlocks the dependent task. Correct its evidence and observe immediate dependency invalidation without retroactively modifying run evidence. Append a corrected method assessment, propose and explicitly APPLY a private parameter change, and verify old runs/templates/approved goal remain pinned while a new run uses the new method.
2. **Authorized AGENT and revocation:** publish a registered read-only AGENT method → start its AGENT Task-bound run → add a prerequisite → verify task readiness blocks stage admission → user confirms prerequisite → schedule exact approved operation → correct prerequisite → verify P0 refuses claim → cancel and invalidate the stale operation → reconfirm prerequisite → obtain **new approval** → execute through P0 → reconcile claim-fenced TRUSTED_PLATFORM result → finish run and user-report criterion. The AGENT task and goal must still not silently become completed.
3. **Step correction vs adaptation:** report a real method problem → diagnose and propose a bounded ADAPTIVE private change → correct the step evidence before deciding. The now-stale proposal must not APPLY even if it was previously reviewed; explicit REJECT must remain permitted and no variant or active run definition changes.
4. **Persona lifecycle and persistence:** preserve a partially attempted run across independent database connections, switch the selected persona and verify read/write authority is withdrawn, reselect the original persona and recover exactly the same run, reject an obsolete generation command, and deny access from another user/persona.

Cross-domain tests intentionally use public typed state/command APIs and existing user-approval helpers; **no direct writes to goal/task/procedure/operation/evaluation authoritative rows**. The only provisioning-only SQL inserts create a second persona definition/instance for a lifecycle test. A clean PostgreSQL test database is initialized from the ordered migrations in `tests/conftest.py`.

## Pass/fail rules

- **Required:** all `tests/test_phase3_*.py`, Phase 0 contracts, prior-phase regressions, and Phase 1 live Ollama/PostgreSQL workflow pass at the same PR head. No new assertion may equate a generated instruction, user report or step outcome with independently verified goal attainment.
- **Durability:** the same command replay produces no duplicate event/outbox entry; a subsequent connection reads the committed version and append-only history. Superseding data never erases historical evidence.
- **Authorization:** the only enabled AGENT execution capability is the previously registered read-only `text.statistics.v1`; all other tool operations still require separately designed admission. No automatic shared procedure update, unauthorized strategy change, or task/goal completion is introduced.
- **Failure handling:** stale versions, changed prerequisites, cancelled operations and generation mismatches must reject *before* mutating protected state. Race regressions from P3-02/P3-04/P3-05 remain mandatory; P3-06 reuses them rather than reimplementing a scheduler.
- **Review:** PR must pass CI and independent Codex review. No merge before explicit user approval.

## Explicit exclusions / subsequent phases

Phase 3 guarantees the **implemented** contract boundary, not complete general-purpose AGENT autonomy: browser/repository edits and external side effects still need per-capability permission, effect reconciliation and cancellation. Phase 4 adds Background scheduling and alignment admission, Phase 5 extends multimodal response/delivery semantics, and Phase 6 extends longitudinal evaluation and confidence calibration. Do not infer those capabilities from this acceptance gate.

**Milestone exit condition:** P3-01 through P3-05 code is demonstrably consistent under normal, corrected, interrupted and unauthorized cross-domain workflows, with no open acceptance-blocking bugs and a reviewable P3-06 test + evidence report.
