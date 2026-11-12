# P3-03 — Procedure Library & Versioning (Architecture v2.5.1)

**Authority:** `Persistent Persona Agent Architecture v2.5.1` §§2.1, 6.3, 9, 18.3 and 21.3–21.4 (PROC-ARCH-01–10). This increment implements definition/version/personalization contracts only. P3-04 owns `ProcedureRun`, step status, dialogue linkage and actual execution.

## Boundaries and ownership

The **Procedure Manager** owns reusable methods. Approved **persona-global** shared templates and **private User × Persona** variants are distinct records. The Goal Engine continues to own outcome and selected strategy; Task domain owns task dependency/actionability; P0 owns operation admission and tool execution. An informational-only procedure lookup does not require creating a goal, task or run.

- `procedure_templates` and immutable `procedure_template_versions`: identity scoped to a persona definition; current approved version; review note, source and publisher; exact preconditions, purpose, applicability, limitations, typed parameter schema, ordered steps, declared capabilities and success tests. Published versions contain SHA-256 checksums and cannot be updated/deleted through normal SQL.
- `procedure_publish_grants`: an **out-of-band platform-provisioned** authorization table keyed by (authenticated user, persona definition). No persona API can issue its own publishing grant. A publication requires a matching active grant, selected Foreground persona generation, a source turn, separate action-bound approval, expected-current-version CAS, event, receipt and outbox. A transaction-scoped advisory lock keyed by the template UUID is acquired **before** the current-version lookup; this serializes first-version creation even when no template row exists or publishers use different event streams. A losing concurrent version-zero request returns `VersionConflict`, not a raw database uniqueness error. The lock is automatically released on commit or rollback. Publisher review note is required. This initial gate is not a complete content-review workflow: review quality, safety audits and privacy-approved shared promotion are still human/operator responsibilities.
- `personalized_procedures`: private variant identity optionally linked to a **currently selected** goal strategy, without modifying the approved goal. `personalized_procedure_revisions` append immutable explicit versions, exact shared `(template_id, version)` pin, validated parameters, bounded private instruction overrides, evidence/approval, and event. Reads are selected-persona fenced. Goal-linked variants require a defensible **intended contribution** and are stored with `suitability_status=UNREVIEWED`, never inferred as suitable merely because a strategy was selected. The full goal ancestor chain is SHARE-locked during link creation and revision to prevent concurrent parent pauses from validating stale lineage.
- Private revision updates use `expected_revision` CAS. Shared publication updates only the latest-version pointer, never pinned variants. A user may explicitly revise to a different version, including an older reviewed version; this is the rollback mechanism. Historical template versions and private revisions remain available.
- `list_shared_procedures` and `list_personalized_approaches` offer bounded discovery under the selected-persona scope; exact versions/parameters are loaded with `read_shared_procedure` or `read_personalized_approach`. Shared discovery never exposes publisher-private source turns.

## Procedure adaptation policy

| Policy | Immutable template | Instance customization |
|---|---|---|
| `STABLE` | Fixed steps and tests | No parameters, no instruction rewrites |
| `PARAMETERIZED` | Fixed steps/tests and parameter schema | Exact typed values for declared parameters |
| `ADAPTIVE` | Published shared version stays immutable | Parameter values plus at most 4 explicit, user-confirmed **USER** instruction text overrides with a documented reason |

`ADAPTIVE` is **not** blanket permission to change executable AGENT steps, success tests, tool capabilities, or a goal objective. Such changes require a newly reviewed template version or later explicit evaluation/authorization. Learning from run outcomes and proposing adaptations belong to P3-05.

Parameter validation uses only TEXT, INTEGER, BOOLEAN, CHOICE with bounded values. Placeholders are restricted to simple `{identifier}` references against the declared schema; attribute lookup, indexing and Python format expressions are forbidden. Unknown/missing or invalid values fail before a revision is committed. Stored parameters should be non-secret configuration values; secret-handling and tool resolution will require the gateway when actual runs are introduced.

## Atomicity, authority and evidence

Each successful change uses the existing P0 scoped idempotency receipt, selected Foreground generation lock, `procedure_approval_phrase(command)` from a **separate authenticated turn**, short PostgreSQL transaction and procedure-specific event stream with transactional outbox. Retries replay the saved result; a changed payload with the same idempotency key fails. Private queries can only access the selected persona's variant data; shared library reads are filtered by persona definition.

A published method is a reusable recipe, not evidence of having performed its steps. Creating/revising a variant never marks task, goal or step complete and never grants a capability. No LLM or network call happens while DB locks are held.

## Phase 3 dependencies

- **P3-04**: durable `ProcedureRun` that pins `(variant_id,revision)` or `(template_id,version)`, links a Task and DialogueThread when appropriate, persists NOT_STARTED/INSTRUCTED/ATTEMPTED/USER_REPORTED_DONE/VERIFIED transitions, delivery acknowledgments, and respects P0 task-operation gate.
- **P3-05**: contextual outcome evidence and validation of safe adaptations; no automatic promotion from personal success into global templates. P3-05 must also add the decision process for moving goal-linked method suitability beyond UNREVIEWED; P3-03 never claims semantic alignment from text similarity or a model assertion.
- **P3-06**: 50-turn SSL setup, interruption/recovery, concurrency, context drift and applied permission tests.

Two P3-02 follow-ups remain separate: stale prior DONE report confirmation and explicit correction of USER_CONFIRMED task status. The Procedure Manager does not take ownership of those fixes.

## Acceptance

`tests/test_phase3_procedures.py` covers authorized/unauthorized shared publication, separate action confirmation, immutable version history, automatic pinning isolation, explicit variant upgrades, strict type/bound checks, adaptation guardrails, private scope across users/personas, optional goal-strategy linkage, concurrent first-version publication and shared version/variant CAS, event/outbox, and idempotent replay. The existing PostgreSQL and live Ollama CI must pass at the PR head before merge.

**Operational note:** granting a publisher is a privileged administrative provisioning action, deliberately not provided in this PR. Procedure publication is for explicitly reviewed content only. A dedicated audited admin interface can be built later when required; a generic TrustedActor alone is not publication authority.
