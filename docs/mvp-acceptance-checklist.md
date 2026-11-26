# Persistent Persona Agent v2.5.1 — MVP acceptance checklist

**Baseline:** 24 November 2026, `main` at `d5d1c8eb06c7f73e8e32a0714eb34eaa22c36586` ([PR #104](https://github.com/BetrayedByEthanol/Chatbot/pull/104) merged).  
**Authority:** Architecture v2.5.1, §§14, 14.1, 14.2, 18, 24.1; reconciled master user-story backlog, 22 November 2026.  
**Companions:** [Implementation matrix](user-story-implementation-matrix.md), assessed at runtime baseline `9c2ce5c47d7e6ba95192a45e146b1571fc591215`; [Development roadmap](development-roadmap.md).  
**Disposition:** initial checklist for review, not evidence of release acceptance or an architecture amendment.

## 1. Scope and acceptance decisions

Phase 0–1 is the initial architectural text MVP. Of 43 Phase 0–1 stories, 28 are confirmed or mixed-authority; 15 are proposed-only. Only the confirmed core of mixed stories is mandatory. Full source acceptance criteria and legacy verification candidates remain controlling in the master backlog. This checklist does not adopt new proposal details.

| Gate | Evidence required | Baseline disposition |
|---|---|---|
| A — Phase 0 contracts and safety | §14 foundation gate and confirmed story criteria | Prior contract CI successful; criterion-level acceptance open |
| B — Phase 1 responsive text core | §14 live tool/restart gate and confirmed story criteria | Prior live technical CI successful; expanded behavior checks open |
| Q — Conversational quality | Actual target-model output, independent review and measured performance | Pending complete, accepted evidence and agreed targets |
| D — Deployment readiness | Applicable identity, permission, data and operational controls | Remote/untrusted deployment blocked; actual scope undecided |

Architecture §14 exit gates remain unchanged:
- Phase 0: switch isolation, command idempotency, event replay, denial of unauthorized actions and competing worker claims pass.
- Phase 1: immediate small-tool answer; slow tool returns later without blocking new turns; restart restores operation correlation.

Q is a practical quality acceptance procedure from roadmap MVP-04 and confirmed conversational requirements (016, 024, 090), with cross-cutting evidence from 088; not a new Phase 1 story. D is a distinct deployment decision. Do not equate the trusted local synthetic harness with an authenticated real-user service or treat later-phase features as prerequisites for the initial architecture gate.

**Evidence rule:** unchecked means not accepted in this register, even where historical tests passed. PASS requires evidence for the applicable criterion and candidate; FAIL records observed failure; BLOCKED records a missing prerequisite; NOT RUN means no execution evidence. N/A requires an explicit scope rationale and may not waive a confirmed invariant. I/P/M is the static matrix status (implemented core/partial/missing), not a test result. Record evidence per criterion before checking the parent story.

## 2. Verified historical execution evidence

| Tested revision | CI run | Outcome / limit |
|---|---|---|
| `bc34a75222f7c408e4ff6eab3c68711269c216e4` (PR #102 head) | [Phase 0 contracts 45162923444](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45162923444) | SUCCESS; real PostgreSQL suite, not every story criterion independently verified |
| Same PR #102 head | [Phase 1 live integrated acceptance 45162923410](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45162923410) | SUCCESS; actual Ollama and PostgreSQL transport/tool/restart tests, `qwen2.5:0.5b`, not target-model conversational quality |
| `43fa8aa369c08eaefb0a428562537abf2be28c5b` (PR #104 head) | [Phase 0 contracts 45463809480](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45463809480) | SUCCESS; documentation PR, not a new Phase 1 live run |

Verified workflow metadata on 24 November 2026. Phase 1 CI is path-filtered. These runs are evidence for the tested revisions, not a newly executed merge-SHA suite. No new tests or benchmark runs were performed for this document. The previous local 25-turn comparison had an incomplete gateway run and review disagreements, as reported in prior operator work and the roadmap; no final accepted model-quality decision is established here. Private artifacts were not re-reviewed for this checklist.

## 3. Confirmed Phase 0–1 acceptance items

Each of the 28 stories below retains its canonical ID, authority, priority, owner and baseline implementation status. Confirmed criteria are reproduced from the master backlog; mixed criteria are scoped to their confirmed core with proposed details identified explicitly. Evidence links come from the matrix and are starting points, not proof of full coverage. Review applicable legacy verification candidates before acceptance; unapproved numerical parameters are not binding.

### A — Phase 0 contracts and safety

15 stories; I/P/M 9 / 6 / 0. All story checkboxes remain open pending explicit verification.

- [ ] **PPA-US-001 — Isolate each user–persona instance** (Confirmed; P0; matrix I)
  - Distinct (user_id, persona_id) instances persist separately.
  - Cross-instance reads and writes are rejected without explicit authorization.
  - Owner: Persona Runtime. Evidence: [phase0_postgres](../tests/test_phase0_postgres.py); [phase0_lifecycle](../tests/test_phase0_lifecycle.py); [phase0-01](./phase0-01.md); [phase0-02](./phase0-02.md)
  - Remaining evidence/limitation: Scoped instance and cross-tenant rejection; extend ingress authentication to deployed host.
- [ ] **PPA-US-002 — Enforce one selected execution authority** (Confirmed; P0; matrix I)
  - Selecting a new instance revokes the prior selection generation.
  - Late old-generation writes and dispatches fail fenced checks.
  - Owner: Persona Runtime. Evidence: [phase0_lifecycle](../tests/test_phase0_lifecycle.py); [phase0_operations](../tests/test_phase0_operations.py); [phase0-02](./phase0-02.md); [phase0-04](./phase0-04.md)
  - Remaining evidence/limitation: Selection generation and stale claims fenced; repeat switch/reselect regression at release.
- [ ] **PPA-US-003 — Enforce lifecycle permissions** (Confirmed; P0; matrix I)
  - Foreground prioritizes conversation and disallows independent autonomous goal advancement.
  - Inactive persona cannot perform persona-owned cognition or task execution.
  - Owner: Persona Runtime. Evidence: [phase0_lifecycle](../tests/test_phase0_lifecycle.py); [phase4_integrated_acceptance](../tests/test_phase4_integrated_acceptance.py); [phase0-02](./phase0-02.md); [phase4-07-integrated-acceptance](./phase4-07-integrated-acceptance.md)
  - Remaining evidence/limitation: Mode and epoch admission enforced for registered work; test new capabilities when added.
- [ ] **PPA-US-004 — Journal authenticated events with provenance** (Confirmed; P0; matrix P)
  - Ingress assigns actor, source, trust, stream sequence and idempotency information.
  - Web, tool and third-party content cannot spoof authenticated-user instructions.
  - Owner: Event System. Evidence: [phase0_postgres](../tests/test_phase0_postgres.py); [phase1_conversation](../tests/test_phase1_conversation.py); [phase5_ingress](../tests/test_phase5_ingress.py); [phase0-01](./phase0-01.md); [phase5-01-multimodal-boundary](./phase5-01-multimodal-boundary.md)
  - Remaining evidence/limitation: Trusted actor provenance and event journal exist; network authentication and third-party ingress are not delivered.
- [ ] **PPA-US-005 — Deliver events through a transactional outbox** (Confirmed; P0; matrix I)
  - Domain update, event and outbox entry commit atomically.
  - Independent consumers use durable deliveries, retries and idempotent acknowledgments.
  - Owner: Event System. Evidence: [phase0_event_delivery](../tests/test_phase0_event_delivery.py); [phase0_acceptance](../tests/test_phase0_acceptance.py); [phase0-03](./phase0-03.md); [phase0-05](./phase0-05.md)
  - Remaining evidence/limitation: Transactional outbox, independent subscriptions and replay; repeat crash and deduplication suite.
- [ ] **PPA-US-006 — Protect domain-owned state with versioned commands** (Confirmed; P0; matrix I)
  - Commands carry actor, scope, evidence, expected versions and idempotency key.
  - Same-entity version conflicts reject or reconcile while independent domain writes succeed.
  - Owner: Domain Infrastructure. Evidence: [contracts](../tests/test_contracts.py); [phase0_postgres](../tests/test_phase0_postgres.py); [phase3_goals](../tests/test_phase3_goals.py); [phase0-01](./phase0-01.md); [phase3-06](./phase3-06.md)
  - Remaining evidence/limitation: Owner commands and versioned idempotent writes; continue cross-domain fence regression.
- [ ] **PPA-US-007 — Schedule durable fenced operations** (Confirmed; P0; matrix I)
  - Operation state, dependencies, leases, claim generations and checkpoints persist.
  - Competing workers produce one valid claim; stale claimants cannot commit.
  - Owner: Execution System. Evidence: [phase0_operations](../tests/test_phase0_operations.py); [phase0_acceptance](../tests/test_phase0_acceptance.py); [phase0-04](./phase0-04.md); [phase0-05](./phase0-05.md)
  - Remaining evidence/limitation: Durable claims and recovery; external exactly-once execution is not claimed.
- [ ] **PPA-US-008 — Reserve interactive resources and bound background use** (Confirmed; P0; matrix I)
  - P0 retains interactive capacity while P1 and P2 use bounded reservations.
  - New autonomous work is blocked if current mode, budget or task gate is invalid.
  - Owner: Execution System. Evidence: [phase0_operations](../tests/test_phase0_operations.py); [phase2_acceptance](../tests/test_phase2_acceptance.py); [phase4_autonomy](../tests/test_phase4_autonomy.py); [phase0-04](./phase0-04.md); [phase4-07-integrated-acceptance](./phase4-07-integrated-acceptance.md)
  - Remaining evidence/limitation: Resource and priority gates implemented; target hardware contention remains a performance gate.
- [ ] **PPA-US-009 — Validate permissions and side effects before execution** (Confirmed; P0; matrix P)
  - Requested tool operation checks capability, permission, scope and side-effect class.
  - Consequential dispatch rechecks authorization after any delay or revocation.
  - Owner: Authorization. Evidence: [phase1_tools](../tests/test_phase1_tools.py); [phase3_agent_operations](../tests/test_phase3_agent_operations.py); [phase4_background_admission](../tests/test_phase4_background_admission.py); [phase0-04](./phase0-04.md); [phase4-03-align01](./phase4-03-align01.md)
  - Remaining evidence/limitation: Registered scoped read-only execution; broaden explicit authorization and test revocation for consequential external effects.
- [ ] **PPA-US-010 — Expose provider-neutral capability contracts** (Confirmed + proposed detail; P0; matrix P)
  - Typed provider-neutral gateway contracts express the necessary arguments/results, modality, resource, cancellation and permission requirements for supported routes. Expanded descriptor design is proposed.
  - Model output and tool results cannot directly grant permissions or mutate another domain.
  - Owner: Model/Tool Gateway. Evidence: [phase1_model_gateway](../tests/test_phase1_model_gateway.py); [phase1_tools](../tests/test_phase1_tools.py); [phase1-02](./phase1-02.md)
  - Remaining evidence/limitation: Typed adapters exist; generalized provider capability, modality and resource selection incomplete.
- [ ] **PPA-US-011 — Provide migrations, reproducible startup and safety tests** (Confirmed; P0; matrix I)
  - Schema migration and development bootstrap create persistent records from an empty database.
  - Isolation, idempotency, replay and claim-fencing tests run in CI.
  - Owner: Runtime Infrastructure. Evidence: [phase0_acceptance](../tests/test_phase0_acceptance.py); [phase0_postgres](../tests/test_phase0_postgres.py); [phase0-05](./phase0-05.md)
  - Remaining evidence/limitation: Ordered PostgreSQL migrations and CI regression suite; deployment bootstrap remains separate.
- [ ] **PPA-US-012 — Retain causal traces and operational measurements** (Confirmed; P0; matrix P)
  - Correlate ingress, context, inference, commands, operations and delivery revisions.
  - Capture p50/p95 timings and queues without indiscriminately logging sensitive payloads.
  - Owner: Shared Infrastructure. Evidence: [phase1_integrated_acceptance](../tests/test_phase1_integrated_acceptance.py); [mvp01f1_acceptance_diagnostics](../tests/test_mvp01f1_acceptance_diagnostics.py); [phase1-integrated-acceptance](./phase1-integrated-acceptance.md); [model-comparison](./model-comparison.md)
  - Remaining evidence/limitation: Correlated timings exist; streaming first content, token usage and complete resource budgets not measured.
- [ ] **PPA-US-104 — Protect stable persona role and behavioral authority** (Confirmed; P0; matrix P)
  - Stable role, purpose, commitments and authority constraints change only via authenticated authorized configuration revision.
  - Mood, context-specific preferences and generated thoughts cannot modify role contract or grant additional permissions.
  - Reload, persona switch and long conversation preserve approved role constraints and scope boundaries.
  - Owner: Persona Runtime. Evidence: [phase0_postgres](../tests/test_phase0_postgres.py); [mvp01f4_capability_claim_enforcement](../tests/test_mvp01f4_capability_claim_enforcement.py); [phase0-01](./phase0-01.md); [mvp-01f4-capability-claim-enforcement](./mvp-01f4-capability-claim-enforcement.md)
  - Remaining evidence/limitation: Persona definition separated from instance changes; full authorized behavioral role revision contract and drift testing incomplete.
- [ ] **PPA-US-111 — Never optimize away required authorization or corrections** (Confirmed; P0; matrix I)
  - Mandatory permission, lifecycle, cancellation and owner-version checks happen before any affected execution or response that depends on them.
  - Cost-based omission or delayed cognition affects only optional processing; blocked mandatory updates cannot be silently skipped.
  - Owner: Authorization + Domain Infrastructure. Evidence: [phase0_operations](../tests/test_phase0_operations.py); [correction_precedence](../tests/test_correction_precedence.py); [phase4_preparation](../tests/test_phase4_preparation.py); [phase0-04](./phase0-04.md); [phase4-02b](./phase4-02b.md)
  - Remaining evidence/limitation: Mandatory recognized authority/correction fences are not optional optimization; broaden intent coverage under 019.
- [ ] **PPA-US-117 — Preserve evidence trust and verification status through derivation** (Confirmed + proposed detail; P0; matrix P)
  - Derived records preserve applicable source, trust, evidence class and revision lineage; the expanded common derivation schema is proposed.
  - Repeated model interpretations do not count as independent corroboration or authorization.
  - User self-report establishes what was said, not independently verified external action.
  - Owner: Event + Memory + Cognitive. Evidence: [phase2_cognition](../tests/test_phase2_cognition.py); [phase2_memory](../tests/test_phase2_memory.py); [phase3_goals](../tests/test_phase3_goals.py); [phase2-06](./phase2-06.md); [phase3-01](./phase3-01.md)
  - Remaining evidence/limitation: Source provenance and epistemic separation implemented across key owners; comprehensive derivation-lineage/verification contract and tests incomplete.

### B — Phase 1 responsive core

13 stories; I/P/M 4 / 9 / 0. All story checkboxes remain open pending explicit verification.

- [ ] **PPA-US-016 — Deliver responsive text conversation** (Confirmed; P0; matrix P)
  - Simple greetings can use one model inference or a deterministic response.
  - First answer does not await irrelevant memory, emotion or goal jobs.
  - Owner: Conversation System. Evidence: [phase1_conversation](../tests/test_phase1_conversation.py); [wide_preference_recall_postgres](../tests/test_wide_preference_recall_postgres.py); [phase1-integrated-acceptance](./phase1-integrated-acceptance.md); [model-comparison](./model-comparison.md)
  - Remaining evidence/limitation: Responsive text paths exist; independent real-model naturalness and latency acceptance outstanding.
- [ ] **PPA-US-017 — Resolve short replies from pending dialogue** (Confirmed; P0; matrix P)
  - A pending question is resolved from active context when unambiguous.
  - Ambiguity triggers a natural clarification instead of guessed intent.
  - Owner: Conversation System. Evidence: [phase1_dialogue](../tests/test_phase1_dialogue.py); [mvp01c_dialogue](../tests/test_mvp01c_dialogue.py); [mvp01f2_conversation_intelligence](../tests/test_mvp01f2_conversation_intelligence.py); [phase1-03](./phase1-03.md); [mvp-01f2-conversation-intelligence](./mvp-01f2-conversation-intelligence.md)
  - Remaining evidence/limitation: Bounded pending-act resolution; broaden ambiguous short-reply and implicit-reference tests.
- [ ] **PPA-US-018 — Assemble only response-relevant context** (Confirmed; P0; matrix I)
  - Mandatory persona, permission and recent conversation context is included.
  - Historical or cognitive context is selected under token and latency budgets.
  - Owner: Context Builder. Evidence: [phase2_acceptance](../tests/test_phase2_acceptance.py); [mvp03b1_context_intelligence](../tests/test_mvp03b1_context_intelligence.py); [mvp03b3_longitudinal_context](../tests/test_mvp03b3_longitudinal_context.py); [phase2-05](./phase2-05.md); [mvp-03b3-longitudinal-context-evaluation](./mvp-03b3-longitudinal-context-evaluation.md)
  - Remaining evidence/limitation: Selective bounded context assembled; independent quality evaluation remains separate.
- [ ] **PPA-US-019 — Capture explicit instructions and restrictions promptly** (Confirmed; P0; matrix P)
  - Clear user instruction is committed by the owning domain or affected operation gated.
  - Delayed cognitive processing cannot supersede the newer instruction.
  - Owner: Conversation System. Evidence: [correction_precedence](../tests/test_correction_precedence.py); [phase4_preparation](../tests/test_phase4_preparation.py); [phase2-07](./phase2-07.md); [phase4-02b](./phase4-02b.md)
  - Remaining evidence/limitation: Narrow explicit corrections and critical gates; arbitrary actionable restrictions need owner routing or clarification.
- [ ] **PPA-US-020 — Complete a fast tool request inline** (Confirmed; P0; matrix I)
  - Eligible fast tool is called within the bounded response budget.
  - Its verified result is included without claiming any unrelated action occurred.
  - Owner: Tool Gateway / Conversation. Evidence: [phase1_tools](../tests/test_phase1_tools.py); [mvp01d_response_decisions](../tests/test_mvp01d_response_decisions.py); [phase1-02](./phase1-02.md)
  - Remaining evidence/limitation: Clock tool inline path; extend deadline and fast-to-async coverage for future capabilities.
- [ ] **PPA-US-021 — Continue slow user-requested work asynchronously** (Confirmed; P0; matrix I)
  - Admitted work persists as FOREGROUND_ASYNC with a correlated operation ID.
  - A restart restores the operation and the user's next message remains responsive.
  - Owner: Execution / Conversation. Evidence: [phase1_tools](../tests/test_phase1_tools.py); [phase1_integrated_acceptance](../tests/test_phase1_integrated_acceptance.py); [phase1-integrated-acceptance](./phase1-integrated-acceptance.md)
  - Remaining evidence/limitation: Durable slow analysis path supports concurrent turns and recovery; broader research is not enabled.
- [ ] **PPA-US-022 — Deliver late results only when relevant** (Confirmed; P0; matrix P)
  - Completion becomes a durable result event and is not treated as a user message.
  - Response Scheduler sends, defers or suppresses the follow-up based on current relevance.
  - Owner: Communication. Evidence: [phase1_tools](../tests/test_phase1_tools.py); [mvp01d_response_decisions](../tests/test_mvp01d_response_decisions.py); [phase1-02](./phase1-02.md)
  - Remaining evidence/limitation: Result event and contextual follow-up path; generalized send/defer/suppress across channels not complete.
- [ ] **PPA-US-023 — Persist response turns and delivery correlation** (Confirmed; P0; matrix I)
  - User turn and response revision are durably recorded with causal operation links.
  - After restart, current conversation and relevant pending response status are reconstructed.
  - Owner: Conversation System. Evidence: [phase1_conversation](../tests/test_phase1_conversation.py); [phase1_integrated_acceptance](../tests/test_phase1_integrated_acceptance.py); [phase1-integrated-acceptance](./phase1-integrated-acceptance.md)
  - Remaining evidence/limitation: Durable turn/revision/delivery correlation and restart; network delivery proof needed for actual client.
- [ ] **PPA-US-024 — Keep speaking concise and nonrepetitive** (Confirmed + proposed detail; P0; matrix P)
  - Routine answers remain short while technical content stays complete and natural. Avoid unnecessary repetition.
  - If optional cadence or anti-repetition hints are used, they must not invent emotional diagnoses or change factual claims. The proposed hint mechanism is not required.
  - Owner: Conversation / Communication. Evidence: [increment3_preferences_procedure_discussion](../tests/test_increment3_preferences_procedure_discussion.py); [wide_preference_recall_postgres](../tests/test_wide_preference_recall_postgres.py); [model-comparison](./model-comparison.md); [mvp-03c-natural-dialogue-evaluation](./mvp-03c-natural-dialogue-evaluation.md)
  - Remaining evidence/limitation: Precise length preferences and direct recall; natural brevity/anti-repetition quality still unaccepted.
- [ ] **PPA-US-090 — Interpret conversational intent and pragmatic meaning** (Confirmed; P0; matrix P)
  - Interpret direct questions, implicit references, negation, hypotheticals and candidate sarcasm against active dialogue state.
  - Use cheap deterministic handling first, optional calibrated classification only when useful, and one main conversational inference when needed.
  - For consequential unresolved ambiguity, request clarification before an irreversible action; do not treat uncertain sentiment as fact.
  - Owner: Conversation System. Evidence: [mvp01a_intent](../tests/test_mvp01a_intent.py); [mvp01b_model_intent](../tests/test_mvp01b_model_intent.py); [mvp01f2_conversation_intelligence](../tests/test_mvp01f2_conversation_intelligence.py); [mvp-01f2-conversation-intelligence](./mvp-01f2-conversation-intelligence.md)
  - Remaining evidence/limitation: Bounded intent paths; broad negation, sarcasm, hypotheticals and ambiguity evaluation incomplete.
- [ ] **PPA-US-091 — Maintain trusted timezone and elapsed interaction context** (Confirmed; P0; matrix P)
  - Obtain trusted time, user timezone, elapsed time since prior message and session, and resolve relative date/time expressions into a timezone-aware stored value.
  - Never treat tool callbacks, timers, secondary participants or heartbeats as primary-user presence.
  - Time context may affect greeting and relevance, but no timestamp alone grants authority to start work.
  - Owner: Conversation System. Evidence: [phase1_tools](../tests/test_phase1_tools.py); [phase0_lifecycle](../tests/test_phase0_lifecycle.py); [phase0-02](./phase0-02.md); [phase1-02](./phase1-02.md)
  - Remaining evidence/limitation: Clock and trusted presence timestamps exist; per-turn elapsed interaction context and natural relative-time resolution incomplete.
- [ ] **PPA-US-105 — Distinguish observations, instructions and correction authority** (Confirmed; P0; matrix P)
  - For state-changing claims, distinguish instruction, external observation, correction claim and hypothetical; source credibility and actor authority remain independent.
  - An unauthorized player cannot overwrite an accepted game roll, while a genuine arithmetic error may be corrected through the owning state command.
  - A user observation conflicting with a weather API is considered with time/location uncertainty, without automatic agreement or dismissal.
  - Owner: Conversation + Domain Owners. Evidence: [mvp01f2_conversation_intelligence](../tests/test_mvp01f2_conversation_intelligence.py); [mvp01f3_conversation_grounding](../tests/test_mvp01f3_conversation_grounding.py); [mvp-01f2-conversation-intelligence](./mvp-01f2-conversation-intelligence.md); [mvp-01f3-conversation-grounding](./mvp-01f3-conversation-grounding.md)
  - Remaining evidence/limitation: Source-grounded distinctions and owner gating; explicit conflicting observations/D&D acceptance not located.
- [ ] **PPA-US-113 — Scale retrieval and analysis until evidence is sufficient** (Confirmed + proposed detail; P1; matrix P)
  - Start with the smallest sufficient context, retrieval or tool step.
  - Stop when evidence is sufficient; escalate targeted gaps or clarify/defer if budget or safety blocks further work.
  - The recorded sufficiency/stop-decision schema and expanded evaluation detail are proposed, not mandatory.
  - Owner: Context Builder + Execution. Evidence: [mvp03b1_context_intelligence](../tests/test_mvp03b1_context_intelligence.py); [phase2_retrieval](../tests/test_phase2_retrieval.py); [phase2-05](./phase2-05.md); [mvp-03b1-unified-context](./mvp-03b1-unified-context.md)
  - Remaining evidence/limitation: Bounded selective retrieval; adaptive sufficiency/stop records and deep escalation policy not fully implemented.

### Cross-story executable checks

The following are additional verification groupings, not additional user stories. Source test labels ARCH-TEST-R/C refer to Architecture §§14.2/24.1.

- [ ] A-X1 — ARCH-TEST-R01–R05: unrelated writes survive, same-version conflict is fenced, one valid competing claim, dispatched unknown effects reconciled before retry, old persona generation cannot commit. Verify durable PostgreSQL state.
- [ ] A-X2 — Safety under load: recognized corrections, permission revocation, cancellation and stale authority cannot be bypassed by optional-work optimization. Deny unsupported effects; do not introduce a side-effectful provider simply to demonstrate denial.
- [ ] B-X1 — ARCH-TEST-C01/C04/C05: resolve pending acknowledgment cheaply, clarify ambiguous referent, distinguish explicit correction from temporary dislike.
- [ ] B-X2 — ARCH-TEST-C13/C14: topic switch does not implicitly cancel independent admitted work; stale cognitive result cannot overwrite newer instructions or dialogue; follow-up relevance is checked.
- [ ] B-X3 — ARCH-TEST-C15/C16: recorded D&D roll authority, genuine arithmetic correction, local observation versus weather API with source/time/location uncertainty. Use isolated simulated sources.
- [ ] B-X4 — Actual Phase 1 exit: eligible inline clock; admitted slow read-only work runs while next user turn receives a reply; restart restores correlation and pending delivery. Generated-only reply not acknowledged after output failure. Replay is fenced and idempotent at persistence boundary, not exactly-once physical delivery.
- [ ] B-X5 — Time/latency: timezone/DST, elapsed message vs session, no fabricated clock, no optional-work monopolization of interactive resources. Measure queue/latency on selected hardware including contention.

### Reproduction

Use disposable PostgreSQL only. Follow [Phase 0 integration](phase0-05.md), [Phase 1 integrated acceptance](phase1-integrated-acceptance.md), [E2E evaluation](mvp-01e-behavioral-evaluation.md), and [CI definitions](../.github/workflows/phase0.yml), [live workflow](../.github/workflows/phase1-acceptance.yml).

```bash
python -m pip install -e '.[test]'
python -m compileall -q src/persona_agent
# Set TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.
python -m pytest -q
```

Use existing ordered migration/test fixtures, record skips/reasons; a run without psycopg/PostgreSQL is not the database gate. For live acceptance follow the documented disposable acceptance database and Ollama setup, then run `python scripts/phase1_live_acceptance.py` or the existing workflow. Record exact tested SHA, model, environment and results. Add focused tests for missing criteria; do not replace the integrated gates.

## 4. Q — Real-model conversational quality (MVP-04)

MVP-05C.3 candidate evidence: [entity-bound historical evidence and directed relationships](model-comparison.md#mvp-05c3--entity-bound-historical-evidence-and-directed-relationships) binds current Memory revisions/episodes to scoped identity IDs, adds versioned directed pair relations and selects only linked evidence on bounded explicit entity history queries. General semantic linking, dated entity queries, lineage, measured performance and behavioral acceptance remain pending.

MVP-05D.1 candidate evidence: [episode evidence continuity](mvp-05d1-episode-evidence.md) adds exact visible-source validation, bounded verbatim evidence in recall and eligible source dereference with derivation provenance. PostgreSQL CI and behavioral acceptance pending; no I/P/M promotion.

MVP-05C.2 candidate evidence: [conversational entity reports and delivered clarification](model-comparison.md#mvp-05c2--conversational-entity-reports-and-delivered-clarification) admits bounded explicit relationship reports through existing Memory commands and resolves relation-only short replies against delivered, unchanged named choice snapshots. General reference/ambiguity repair, entity relationships and history, measured performance and behavioral acceptance remain pending.

MVP-05C.1 candidate evidence: [scoped entity identities and grounded reference recall](model-comparison.md#mvp-05c1--scoped-entity-identities-and-grounded-reference-recall) adds owner-validated identity, alias and relation revisions, canonical scoped name/alias resolution and immediately adjacent delivered pronoun binding. General conversational entity admission/ambiguity repair, entity-to-entity relations, derived lineage and behavioral acceptance remain pending.

MVP-05B.3 candidate evidence: [immediate report precedence and historical response fencing](model-comparison.md#mvp-05b3--immediate-report-precedence-and-historical-response-fencing) adds prospective exact-slot ingress report projection and atomic scope revision/expiry checks for explicit historical model responses, with one bounded stale-context retry. No new Memory mutation authority or acceptance promotion; broad semantic correction/lineage and measured latency remain pending.

MVP-05B.2 candidate evidence: [bounded dated historical retrieval](model-comparison.md#mvp-05b2--bounded-dated-historical-retrieval) uses trusted receipt and explicit timezone to constrain a small family of explicit topical recollection questions, distinguishes reports from event dates/current facts, and blocks undated fallback for unresolved periods. Broader temporal/semantic ambiguity and independent real-model verification remain pending.

MVP-05B.1 candidate evidence: [shared historical eligibility and correction precedence](model-comparison.md#mvp-05b1--shared-historical-eligibility-and-correction-precedence) shares Memory dispute filtering and conservative exact-source episode exclusion before ranking. Complete owner values and known adjacent correction chains are budgeted atomically. Broader temporal queries, source ambiguity, lineage and independent model verification remain pending.

MVP-05A candidate evidence: [trusted temporal context](model-comparison.md#mvp-05a--trusted-temporal-context) adds source-linked ingress snapshots and scoped relative-time resolution; PPA-US-091 remains partial pending general relative language, user timezone configuration and measured temporal behavior. No new acceptance promotion.

MVP-04D candidate evidence: [responsive tool and terminal outcome paths](model-comparison.md#mvp-04d--responsive-tool-and-terminal-outcome-paths) extends existing P1 tool contracts with terminal failure/cancellation notices, direct tool context and persisted outcome delivery/replay. Existing P0 retries/authority unchanged. Q08 measured target-host latency and independent behavioral acceptance remain pending; no acceptance promotion.

MVP-04C.3 candidate evidence: [exact saved personal-fact grounding](model-comparison.md#mvp-04c3--exact-saved-personal-fact-grounding) adds narrowly routed saved-fact responses and canonical rendering on forced model-only. Q04/Q07 still need real-model/held-out verification and independent review. Frozen benchmark unchanged, expected auto call count now 22; do not treat guarded delivered text as raw model output.

MVP-04C.2 candidate evidence: [contextual replies and correction authority](model-comparison.md#mvp-04c2--contextual-replies-and-correction-authority) adds short-reply compatibility, bounded clarification guidance and correction/source-authority separation. Q04/Q07 and PPA-US-017/090/105 remain pending broader evidence; C15/C16 are conversational fixtures only, with no registered game/weather owner. No acceptance promotion.

MVP-04C first increment: [pragmatic and dialogue safeguards](model-comparison.md#mvp-04c--current-turn-pragmatics-and-dialogue-repair-first-increment) contributes targeted B-X1/B-X2 and Q04/Q05/Q07 evidence. No new acceptance; independent reviews, real-model reruns and owner-authority C15/C16 cases remain open. Model-only retains host safeguards; distinguish raw model behavior from delivered output.

MVP-04B candidate evidence: [grounding and presentation](model-comparison.md#mvp-04b--grounding-and-response-presentation) adds direct clock qualifiers, personal-fact source guidance and narrowly scoped eligible Memory retrieval, plus response-presentation guidance. Q04/Q05/Q07 remain pending real-model verification and adjudication. No new acceptance decision.

MVP-04A tooling evidence: [model comparison diagnostics](model-comparison.md#mvp-04a--review-and-evidence-integrity) provides create-only status reconciliation, reviewer/case/field diagnostics, safe gateway failure categories and bundle fingerprints/host p95. Q01–Q03, Q06 and Q08 benefit from the tooling; no Q gate is accepted without candidate runs, genuine independent evidence and agreed targets. See the linked focused regression tests; execution evidence must be recorded separately.

Reuse [model comparison](model-comparison.md), [natural dialogue review](mvp-03c-natural-dialogue-evaluation.md), and the [frozen 25-turn scenario](../scenarios/persona_model_comparison_v1.json). The 18-message [MVP-03C.1 target-model benchmark](mvp-03c1-target-model-quality-benchmark.md) is a distinct scenario-specific protocol; do not use its exact-scenario evaluator for 25-turn ZIPs. The [25-turn PostgreSQL stub regression](../tests/test_wide_preference_recall_postgres.py) checks persistence/routing, not real output quality.

| Check | Required acceptance evidence | Status |
|---|---|---|
| Q01 Reproducibility | Candidate SHA, migration/scenario identity, model tag and resolved digest (explicitly disclose unavailable), settings, route, hardware and private artifact identity recorded | Pending |
| Q02 Complete contracts | Selected target-model full/auto case delivers 25 messages across 3 sessions/2 restarts, complete valid evidence, no contract errors. Record and diagnose any failed/incomplete case | Pending |
| Q03 Independent review | Two genuinely distinct human reviewers independently complete matching blinded packet scorecards; all dimensions valid 0/1/2, evidence notes for negative ratings/flags | Pending |
| Q04 Grounding/authority | Corrected preference wins, unknown stays unknown, no unresolved critical fabricated fact/time/history/tool claim, unapproved commitment, unauthorized action or ignored explicit boundary in agreed acceptance corpus | Pending |
| Q05 Natural dialogue | Relevant grounding, continuity, calibration, brevity, affect, goal support, agency, coherence and repetition dimensions evaluated; routine replies short, technical detail complete, explicit exceptions respected | Pending |
| Q06 Adjudication | Aggregate independent ratings; review disagreements and critical flags; record adjudication and human acceptance decision separately | Pending |
| Q07 Generalization | Focused preference recall and held-out regressions for reproduced defects, intent, corrections, temporal grounding, interruption; frozen scenario unchanged | Pending |
| Q08 Performance | Actual host p50/p95 response/delivery, queues and resource/latency budget on target hardware; report unavailable fields, distinguish warm/cold and contention where applicable | Pending |

The roadmap proposes zero unresolved critical safety/grounding/authority failures in the agreed acceptance corpus and passing deterministic contracts. Agree measurable human rubric, latency and resource targets before judging the release candidate. No new numerical quality or timing threshold is fixed by this document. Passing the finite corpus does not establish exhaustive safety or general intelligence.

Example comparison (existing models, not an approved winner):

```bash
# Installed test extras, Docker Compose v2, running local Ollama,
# pulled models and configured disposable .env.e2e required.
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both --thinking off \
  --scenario scenarios/persona_model_comparison_v1.json \
  --output-dir artifacts/mvp04a-acceptance-NEW
```

Use a genuinely new output directory. Full/auto is the candidate behavior; model-only intentionally bypasses adaptive routing and is diagnostic. Record every intended case, including failures; incomplete comparisons cannot establish a winner. A selected model may be accepted without a multi-model winner claim. Use [focused recall scenario](../scenarios/preference_recall_v1.json) in a separate run. Keep raw synthetic ZIPs, prompts, model map, review packets and scorecards private; no real user transcript in this fixture.

The reviewer templates start with null scores. Only give each reviewer the blinded packet and matching empty scorecard. Do not reveal model identity or the other reviewer's ratings. Valid critical codes from [review implementation](../src/persona_agent/natural_dialogue_review.py): `fabricated_history`, `unauthorized_action_claim`, `unjustified_commitment`, `unsafe_affect_claim`, `ignored_explicit_boundary`. Narrative criteria belong in evidence notes, not as flag codes; preserve legitimate critical findings rather than deleting/relabeling them to force acceptance.

```bash
python scripts/review_natural_dialogue.py aggregate \
  --packet artifacts/mvp04a-acceptance-NEW/case_01.review.json \
  --first artifacts/mvp04a-acceptance-NEW/case_01.reviewer_a.json \
  --second artifacts/mvp04a-acceptance-NEW/case_01.reviewer_b.json \
  --out artifacts/mvp04a-acceptance-NEW/case_01.aggregate.json
```

Run only for a successful case after completed independent ratings; output path must not exist. Repeat for eligible cases. `REQUIRES_ADJUDICATION` and `INDEPENDENT_REVIEW_COMPLETE` are review statuses, not release acceptance. Host response wall time includes processing and worker overhead, not token throughput or TTFT; do not invent absent metrics.

## 5. D — Deployment readiness (conditional separate gate)

The [local acceptance host](../src/persona_agent/acceptance_host.py) trusts operator identity supplied over stdin/stdout and must not be exposed as a remotely accessible unauthenticated API. MVP-07A/B applicability work can proceed independently. No web framework, identity provider, external processing policy or retention duration is selected here.

- [ ] D01 — Record intended scope: trusted local synthetic testing, local real-user app or remote/multi-user service; identify actor, client, users, providers and storage. State applicability.
- [ ] D02 — For untrusted ingress, server-authenticated identity binds actor/source/selected instance. Reject forged authority and nonprimary presence. Model/tool output cannot authorize commands.
- [ ] D03 — Reconnect/replay, scope isolation, selected-generation fences at dispatch and delivery, revocation/cancellation and denied unsupported capabilities verified for the chosen host.
- [ ] D04 — Inventory personal data, logs, derived records, backups and provider routes; decide applicable consent, access, correction, retention and deletion controls; prevent unnecessary sensitive logs/transfers. No universal legal-compliance claim. Detailed proposals 015/120/121 need explicit decisions, but baseline confidentiality, isolation and corrections cannot be waived.
- [ ] D05 — Validate migration/bootstrap, recovery, least privilege, credentials, operational diagnostics and resource budgets on selected environment. Test only with disposable databases and private artifacts.
- [ ] D06 — Confirm actual release-scope correction/revocation/deletion controls; restrict deployment if necessary controls are absent. Full propagation 118/085 retains Phase 2/6 placement; not all later-phase work is required for local synthetic acceptance.

Current remote/untrusted deployment: BLOCKED (authenticated application boundary absent). Real-user scope/privacy policy undecided. Trusted local synthetic testing remains available. No user-facing deployment acceptance granted.

## 6. Proposed-only stories — decision register

All 15 proposed-only Phase 0–1 stories are included here, not silently promoted. Existing compatible protections remain in place. DEC groups reference [roadmap §5](development-roadmap.md#5-proposal-decision-register); mixed proposed detail also requires an explicit decision.

| Canonical story | Phase | Matrix | Decision |
|---|---:|:---:|---|
| PPA-US-013 — Normalize hints, proposals, owner decisions and commands | 0 | P | DEC-01 — unapproved |
| PPA-US-014 — Prevent uncommitted streaming work claims | 0 | P | DEC-02 — unapproved |
| PPA-US-015 — Define consent, retention, correction and deletion contracts early | 0 | M | DEC-03 — unapproved |
| PPA-US-097 — Distinguish model confidence hints from calibrated confidence | 0 | P | DEC-01 — unapproved |
| PPA-US-110 — Unify response and processing contract vocabulary | 0 | P | DEC-01 — unapproved |
| PPA-US-122 — Namespace requirement and acceptance-test identifiers | 0 | M | DEC-01 — unapproved |
| PPA-US-120 — Review sensitive-data routing to additional providers | 0 | M | DEC-03 — unapproved |
| PPA-US-121 — Define deployment-specific privacy release prerequisites | 0 | M | DEC-03 — unapproved |
| PPA-US-123 — Map named helpers to accountable existing domain owners | 0 | P | DEC-01 — unapproved |
| PPA-US-124 — Keep reference flows and regression tests aligned with reconciled contracts | 0 | P | DEC-01 — unapproved |
| PPA-US-025 — Support provider text, structured and tool-call output safely | 1 | P | DEC-02 — unapproved |
| PPA-US-026 — Acknowledge long work only after admission | 1 | I | DEC-02 — unapproved |
| PPA-US-112 — Route sparse signals to relevant owning domains | 1 | P | DEC-02 — unapproved |
| PPA-US-096 — Select models by eligible capabilities and measured quality | 1 | P | DEC-02 — unapproved |
| PPA-US-108 — Invoke detailed alignment classification only for consequential state changes | 1 | P | DEC-02 — unapproved |

True token streaming (014/025) is not currently implemented by the Ollama gateway; not mandatory for confirmed nonstreaming Phase 1 exit. If adopted, add provisional display vs committed response, admission-before-claim, cancellation, stale-generation fences and actual delivery ACK checks. Continue to enforce existing truthful acknowledgments, typed validation and permission constraints without waiting for proposal adoption.

## 7. Explicit exclusions and acceptance record

Architecture §14.1 excludes recursive goal autonomy, self-developed tools, universal semantic representations, all communication channels, live collaboration and complex external workflows. Full entities/memory, advanced cognition/affect, natural-language goal approval, procedure learning, Background autonomy, voice/social/avatar, regeneration and end-to-end privacy propagation retain their original roadmap phases. Existing thin slices may be exercised for grounding/authority regression without expanding the initial MVP. Actual deployment safety requirements are not deferred by these exclusions.

| Candidate release record | Result |
|---|---|
| Candidate SHA / migration identity | Pending |
| Deployment scope, model, hardware/settings | Pending |
| A result, run references and criterion evidence | Pending |
| B result, live run and criterion evidence | Pending |
| Q result, private artifact fingerprints and source-free adjudication | Pending |
| D result and applicability | Pending |
| Agreed quality/latency/resource targets and measured results | Pending |
| FAIL/BLOCKED/NOT RUN items, owner, remaining work | Pending |
| Residual risks, accepted limitations and decision references | Pending |
| Explicit acceptance decision, reviewer/operator and date | Pending |

For every criterion record ID, source criterion, candidate revision, actual test/run reference, environment/model if applicable, observed outcome, limitations and reviewer/date. Record skipped/failed results. Keep raw private review records outside public repo; publish only approved source-free summaries and evidence identifiers. Do not promote I/P/M based on intent or a planned test. Update matrix when implementation coverage changes.

## 8. Next development work

Start MVP-04A benchmark and review integrity: diagnose actual gateway failure from available evidence, improve validation diagnostics if reproduced, preserve frozen scenario and independent review requirements. Then MVP-04B reproduced grounding fixes, MVP-04C pragmatic/correction checks, MVP-04D tool/late result checks, MVP-05A trusted time. Authentication and applicable data-lifecycle decisions precede untrusted deployment.

**Coverage:** 43 unique Phase 0–1 stories: 28 confirmed/mixed (15 Phase 0; 13 Phase 1; I/P/M 13 / 15 / 0), 15 proposed-only (10 Phase 0; 5 Phase 1; I/P/M 1 / 10 / 4). All 43 canonical IDs appear exactly once as primary checklist/decision entries in §§3/6. This document does not execute tests, benchmarks, human review or deployment acceptance.
