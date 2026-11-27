# Persistent Persona Agent v2.5.1 — Development roadmap

**Planning baseline:** 24 November 2026; `main` at `05d1f5a02510d4f7c720471f0031267876984dc3` (merged [PR #103](https://github.com/BetrayedByEthanol/Chatbot/pull/103)).  
**Architecture authority:** Persistent Persona Agent Architecture v2.5.1 (12 November 2026), especially §14 and §14.1; reconciled master user-story backlog dated 22 November 2026.  
**Traceability:** [User-story implementation matrix](user-story-implementation-matrix.md), initial baseline `9c2ce5c47d7e6ba95192a45e146b1571fc591215`; scoped MVP-05 reassessment at merged PR #124.  
**Status:** proposed implementation sequence for review; not an architecture amendment, a release certification or a schedule commitment.

## 1. Objective and constraints

Deliver a usable, persistent, responsive text persona first, then complete its adaptive intelligence and bounded autonomy. Preserve the established seven architecture phases, the modular monolith, PostgreSQL authority, per-user/persona scope, selected execution generation and domain ownership. The existing implementation reaches parts of later phases; do not rebuild those parts or mistake their availability for acceptance of the earlier phases.

The initial architecture MVP is **Phase 0–1 only**. Memory/cognition (Phase 2) and goals/procedures (Phase 3) are subsequent architecture deliverables; selected thin slices can be tested early to expose real conversational failures, without enlarging the initial MVP exit gate. Recursive autonomous execution, self-developed tools, universal semantic representations, all channels, collaboration and complex external workflows are excluded from the initial MVP (§14.1).

The labels MVP-04–07 below are **continuation work-package identifiers**, not new architecture phases, approved feature scope or a commitment to ship all of these as the initial MVP. Existing milestone documents remain historical contracts and test entry points. Do not rename old milestones or reopen established infrastructure unless a reproduced defect warrants it.

## 2. Baseline and release interpretation

After the scoped 05E reassessment of the 12 MVP-05 stories, the matrix contains 48 implemented-core, 57 partial and 19 missing/not-located stories. Of 92 confirmed or mixed-authority stories, 45 have implemented core, 38 are partial and 9 missing/not located. Other tracks retain the historical static assessment. The other 32 are proposed-only/deferred. These are provisional static classifications, not test results or release percentages. Proposed details within mixed stories also require adoption decisions.

| Architecture phase | Stories | I / P / M | Evidence and remaining gate |
|---|---:|---|---|
| 0 — Contracts & safety | 25 | 9 / 12 / 4 | Durable contracts and PostgreSQL tests exist; complete deployed identity/authorization and operational evidence. |
| 1 — Responsive core | 18 | 5 / 13 / 0 | Local integrated transport and model/tool paths exist; real-model quality, intent and time grounding require verification. |
| 2 — Persistent cognitive state | 25 | 12 / 12 / 1 | Scoped entities and distinct memory categories implemented; natural use, correction lineage and cognitive integration remain open. |
| 3 — Goals & procedures | 20 | 10 / 6 / 4 | Owner contracts and guided runs exist; natural approval, decomposition, evidence and strategy discovery remain open. |
| 4 — Background autonomy | 15 | 9 / 5 / 1 | Preparation, ALIGN-01, preemption, checkpoint and in-app notification contracts exist; real workload and reactivation coverage need completion. |
| 5 — Multimodal & social | 11 | 2 / 5 / 4 | Local audio contracts and adapters exist; host/hardware, channels, participant isolation and avatar remain open. |
| 6 — Evaluation & hardening | 10 | 1 / 4 / 5 | Evaluation and recovery tooling exists; regeneration, privacy propagation, drift and measured acceptance incomplete. |

**Evidence boundaries.** Phase 0 and Phase 1 CI successes previously recorded in the matrix apply to the tested PR head, not a new execution on this baseline. The 25-turn PostgreSQL test uses a stub model. A real-model comparison and independent human assessment are distinct from deterministic contract tests. Previous operator runs reported one gateway failure and qualitative disagreements; no final accepted review, new benchmark execution or verified model winner is established here. Private prompts, transcripts and reviewer materials must not enter the public repository.

## 3. Dependency-aware execution sequence

| Track | Primary architecture phases | Dependencies | Entry status |
|---|---|---|---|
| BASE — Foundation regression | 0; cross-cutting | Existing implementation | Maintain continuously |
| MVP-04 — Conversational quality | 0–1; early evaluation | BASE | Implementation increments merged; independent quality review open |
| MVP-05 — Time, recall and entities | 1–2 | MVP-04 evidence; existing memory | 05A–E.1 merged; technical CI and v2 automated benchmark passed; independent quality review open |
| MVP-06 — Goal and procedure dialogue | 3 | MVP-04; relevant MVP-05 grounding and owner gates | 06A–D merged and technical CI passed; 06D local saved-document research only, external provider open; Phase 3 exit also requires Phase 2 exit |
| MVP-07A — Authenticated application | 0–1 | BASE | 07A local authenticated text boundary merged; mandatory remote/untrusted gate remains open |
| MVP-07B — Data lifecycle and provenance | 0, 2, 6 | BASE; deployment scope decision; owner inventory | 07B1 and six 07B2 increments merged with technical CI; remaining owner-specific lineage and 07B3 lifecycle acceptance open |
| P2-F — Remaining cognitive state | 2 | MVP-05; 07B2 cognitive/derived eligibility and applicable response/raw/workflow lineage; P2-F1.1 admission checks before predictive activation | Required for Phase 2 exit; six bounded P2-F1 increments |
| P4-F — Background autonomy | 4 | Phase 0–3 exit evidence; applicable permissions and resource budgets | Existing slices to harden |
| P5-F — Voice and social | 5 | Phase 0–4 exit evidence; authenticated host; participant policy | Existing audio adapters to integrate |
| P6-F — Evaluation and hardening | 6 | Relevant owner contracts and earlier phase evidence | Continuous regression now; final hardening later |
| DEC-01–07 — Proposal decisions | Various | Explicit approval of individual proposal(s) | Not scheduled as mandatory implementation |

Original recommended order (historical planning baseline): MVP-04A benchmark integrity and failure evidence → MVP-04B reproduced response defects → MVP-04C contextual intent and corrections → MVP-05A trusted time. Start MVP-07A authentication contract and MVP-07B deployment/data inventory in parallel if there is capacity, but never make an untrusted deployment available before their applicable safety gates. Continue with MVP-05B/C memory and entities, P2-F cognitive state and MVP-06 goal/procedure slices according to their dependencies. No release dates or hardware assumptions are fixed. At each milestone review the linked [capability gaps (§9)](#9-capability-gap-review-register); a review checkpoint is not an implementation dependency or expanded MVP gate.

## 4. Implementation increments and exit checks

All IDs in this document use the `PPA-US-` prefix. The crosswalk in §7 assigns each canonical story one primary work package; cross-cutting dependencies may appear more than once in the narrative. For each implementation increment, inspect the current owner API and exact backlog criteria, define the smallest useful contract, add automated tests, then record actual evidence in the matrix. Do not promote a status based on an intended milestone alone.

### BASE — Preserve the foundation

**Capability-gap review checkpoints (not committed implementation):** GAP-15, GAP-18 — see [§9](#9-capability-gap-review-register).

Primary IDs: 001, 002, 003, 005, 006, 007, 008, 011, 111.

- Keep the existing runtime selection, event delivery, versioned commands, operation scheduler/control and ordered migrations. Do not introduce a new broker or competing scheduler.
- Reuse [Phase 0 acceptance](phase0-05.md), [operation contracts](phase0-04.md) and [Phase 2 integration](phase2-08.md). Run PostgreSQL isolation, idempotency, version conflict, worker contention, stale lease, outbox replay, resource reservation and selected-generation tests at relevant changes.
- Exit/ongoing invariant: every selected-user switch invalidates the old authority; repeated command/delivery does not repeat committed effects; unrelated owner updates both survive; competing claims produce one valid worker; externally dispatched unknown outcomes are not retried blindly. Record test revision and run evidence. Keep LLM and provider calls outside DB transactions.

### MVP-04 — Conversational quality and pragmatic understanding

**Capability-gap review checkpoints (not committed implementation):** GAP-02, GAP-03, GAP-07, GAP-08, GAP-13, GAP-14, GAP-18, GAP-19 — see [§9](#9-capability-gap-review-register).

Primary IDs: 012, 104, 016, 017, 018, 019, 020, 021, 022, 023, 024, 090, 105, 088.

Existing anchors: [model comparison](model-comparison.md), [natural dialogue evaluation](mvp-03c-natural-dialogue-evaluation.md), [conversation intelligence](mvp-01f2-conversation-intelligence.md), [grounding](mvp-01f3-conversation-grounding.md), [capability claims](mvp-01f4-capability-claim-enforcement.md), [integrated Phase 1 acceptance](phase1-integrated-acceptance.md). Use existing Conversation, Dialogue, Relationship, Tool and Runtime owners.

| Increment | Deliverable | Demonstrable check |
|---|---|---|
| 04A — Benchmark and review integrity | Reuse frozen 25-turn scenario; reproduce full/model-only comparison on isolated DBs; diagnose failed gateway request; improve validator diagnostics only if needed. | All planned cases produce complete evidence or an explicit recorded failure; 25 delivered turns per successful case, three sessions/two restarts, correct route and model attribution, no contract failures; review invalid score/flag errors name offending case without altering ratings. |
| 04B — Grounding and response presentation | Fix reproduced unsupported time, car/history, preference and procedural claims; distinguish unknown, user-reported and verified. Preserve exact sentence-limit exceptions and natural concise responses. | Focused regressions plus frozen scenario; no fabricated known/unknown facts or tool success in safety-critical fixtures; independently review actual replies. Do not hard-code benchmark text or default to a single universal response length. |
| 04C — Pragmatics and pending dialogue | Improve bounded contextual intent, correction authority, negation, quoted/hypothetical content, short replies and uncertain references using existing interpretation and dialogue contracts. | Thinking aloud does not create tasks/goals; explicit restrictions apply through owning domains; ambiguous requests clarify rather than guess; old observations cannot supersede newer authorized instructions. D&D/weather example and unrelated-topic interruption checked. |
| 04D — Responsive tool and result paths | Confirm inline clock and admitted asynchronous work; preserve correlation, late result relevance and explicit delivery status; keep model output separate from execution authorization. | Slow operation does not block a new turn; restart restores correlation; failed/obsolete operation is not claimed successful; late result is delivered/deferred/suppressed according to authorized relevance. No effectful general tools added here. |

Quality acceptance procedure:
1. Reuse [comparison runner](../scripts/compare_persona_models.py) with the frozen [25-turn scenario](../scenarios/persona_model_comparison_v1.json), identical thinking and model settings for comparable runs, isolated test databases and new private output directories. If comparing the prior Qwen 2.5 7B / Qwen 3.5 9B, use both full and model-only as documented; this is a comparison, not a model-selection decision.
2. Run focused [preference recall scenario](../scenarios/preference_recall_v1.json) and [real PostgreSQL stub regression](../tests/test_wide_preference_recall_postgres.py). Track baseline and held-out failures separately. Report completion, contract outcome, observed routes, host latency p50/p95 where available, lengths and actual measurement limits; host latency is not TTFT or token throughput.
3. Two genuinely independent human reviewers score blinded packets with the existing [review script](../scripts/review_natural_dialogue.py), using 0/1/2, valid critical codes, evidence notes and adjudication. Do not replace absent human assessments with model ratings. Retain numerical, source-free findings and verified provenance; keep raw synthetic evidence private.
4. Proposed acceptance rule: no unresolved critical safety/grounding/authority failures in the agreed acceptance corpus and all deterministic contracts pass. Human rubric targets, p50/p95 latency budgets, target hardware and model identity must be agreed before a release pass; no arbitrary numerical quality threshold is set here.

### MVP-05 — Trusted temporal context, retrieval and entity continuity

**Capability-gap review checkpoints (not committed implementation):** GAP-04, GAP-05, GAP-07, GAP-08, GAP-09, GAP-12 — see [§9](#9-capability-gap-review-register).

Primary IDs: 091, 113, 029, 030, 031, 034, 093, 027, 028, 032, 033, 092.

| Increment | Deliverable | Demonstrable check |
|---|---|---|
| 05A — Trusted time | Add bounded response-relevant local time/offset and interaction anchors from trusted clock, configured timezone and persisted timestamps; clarify missing timezone or unresolvable dates. | DST/timezone changes; last message/session distinction; uncertain dates and first contact; relative-date queries; no invented clock or elapsed duration. Retain inline clock route and normal fast path. |
| 05B — Historical recall and correction | Extend existing bounded source-linked retrieval and correction precedence; combine relevant active threads, recent turns, summaries and authoritative fact/preference evidence. | Exact preference and personal meaning recalled across sessions/restarts; corrected records dominate, unknown remains unknown; unrelated topics excluded; compression retains original source; cross-user/persona retrieval denied. |
| 05C — Scoped entities | Design minimal Memory-owned identity, aliases and evidence-linked relations; resolve mentions with uncertainty without a general graph platform. Define version/correction rules before migration. | Same alias referring to different entities does not merge them; corrected relations are reflected; ambiguous pronouns trigger clarification; unrelated entities are not retrieved. No model inference under DB locks. |
| 05D — Memory coverage | Complete source-linked episodes, recurring patterns and document knowledge as distinct types; extend user-defined semantic meanings without treating a summary as a fact. | Recall a supplied procedure or meaning with source and epistemic status, reject unsupported inference, preserve independently stored facts under compression. No universal semantic representation required. |

05D.1 candidate implementation (26 Nov 2026): [episode evidence continuity](mvp-05d1-episode-evidence.md) reuses P2-02 extraction and 05B/C recall eligibility, validates exact model-visible quotations, supplies short verbatim evidence within context budgets and guards direct evidence dereference. No acceptance promotion; multi-source experiences, patterns, semantic definitions and document knowledge remain follow-ups.

05D.2 candidate implementation (26 Nov 2026): [meta-episodic memory](mvp-05d2-meta-episodic-memory.md) adds scoped model-interpreted pattern versions, explicit support/counterepisode links, bounded candidate discovery in existing cognition and fenced explicit recall. Exact-claim deduplication and read-time invalidation reuse historical revision/eligibility contracts. Semantic merging, full lineage and real-model evaluation remain open.

05D.3 candidate implementation (26 Nov 2026): [semantic memory and personalized meanings](mvp-05d3-personal-meanings.md) extends existing PERSONAL_MEANING admission with bounded explicit definitions, revision-pinned constituent spans and separate rule-inferred concept links. Exact recall is canonical and fenced; bounded routine-use context preserves user definition precedence. General free-form definitions and cognitive associations remain open.

05D.4 candidate implementation (26 Nov 2026): [document and procedural knowledge](mvp-05d4-document-knowledge.md) adds explicitly approved private supplied-text versions, exact bounded passages and pinned private procedure references. Canonical source recall reuses the historical read fence; source text is not executable instruction or model evidence. External ingestion, semantic synthesis and full retention/revocation remain follow-ups.

05D.5 candidate implementation (26 Nov 2026): [MetaMemory and retrieval feedback](mvp-05d5-metamemory.md) adds recall-driven gap history, delivered-choice suppression, evidence-bound user feedback and exact-query/document-version ranking demotion. GAP-04/09 partial evidence; cognitive proposals, broad feedback and held-out evaluation remain follow-ups.

05E — [Integrated acceptance and consolidation](mvp-05e-memory-acceptance.md): 05A–D merged. Reuse source/owner fences for unsupported known-topic definitions, fix clarification cooldown from actual question delivery, add a cross-store 50-turn compression/restart regression and a real-model review scenario. Candidate tests NOT RUN. 7 I / 5 P / 0 M after scoped reassessment; no architectural phase exit or behavioral acceptance. Focused 06A may follow passing technical CI without waiting for deferred P2-F or remote deployment gates. Source-qualified document use is a targeted dependency for 06B/D when required, not a new knowledge architecture.

05E.1 — [Correction routing and conversational calibration](mvp-05e1-correction-routing.md): verified PR #125 technical CI (2,150 passed, 3 skipped) and operator's four real-model v1 runs (68 delivered, zero runtime errors; independent review not completed). Reuse prepared historical fence and canonical commit to bypass inference for persisted unsupported definition reports and unresolved routine-use requests; distinguish understood conversation from saved definition. Extend a versioned v2 scenario with exact canonical reply/auto-route checks and reconfirmation. PR #126 subsequently merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45801307151) passed (2,154 passed, 3 skipped) and v2 operator benchmark completed 4/4 cases (80 delivered, 220/220 automated checks). Independent quality review remains open. No new goal/Memory authority or model selection.

Reuse [retrieval](phase2-05.md), [unified context](mvp-03b1-unified-context.md), [longitudinal context](mvp-03b3-longitudinal-context-evaluation.md), [memory eligibility](mvp-02c12-source-grounded-eligibility.md) and [Phase 2 integration](phase2-08.md). Use existing `prepare_text_context`, `memory_retrieval`, `historical_user_turns` and dialogue ownership; no second context builder. 05A is a Phase 1 requirement; 05B–D belong to Phase 2. The confirmed core of 113 is selective adequate retrieval; proposed stop-reason schemas/escalation policy are not automatically approved. For correction across derived stores, depend on MVP-07B.

### P2-F — Finish cognitive state before the Phase 2 exit

**Capability-gap review checkpoints (not committed implementation):** GAP-01–06, GAP-10, GAP-12, GAP-13, GAP-20 — see [§9](#9-capability-gap-review-register).

Primary IDs: 036, 106, 035, 037, 038, 039, 040, 041, 094.

**P2-F1 — Cognitive continuity and predictive cognition.** Adopted [PRED-ROADMAP-01](predictive-user-modeling-refinement.md) (26 Nov 2026), refined by the owner-approved 27 Nov review. Preserve watermarked asynchronous reflections, domain ownership and versioned belief correction. The subdivisions below are reviewable implementation increments, not new architecture phases, canonical stories, primary assignments or phase exit gates. OPEN gap checkpoints require a disposition, not blanket implementation; separate DEC decisions remain in force.

| Increment | Deliverable | Demonstrable check / checkpoint |
|---|---|---|
| P2-F1.1 — Cognitive continuity and admission | Reuse cognitive lineage, pattern eligibility and owner commands; revisit unresolved questions, competing hypotheses and corrections across ranges. Specify predictive admission before persistent learning. | Restart, interruption, correction, stale-worker replay, independent owner writes and admission fixtures. GAP-06; 035/036/106/117/118. |
| P2-F1.2 — Contextual expectations | Minimal scoped versioned expectations, comparable evidence, maturity, counterexamples and drift. | Two histories, cold start, duplicate evidence, context change, expiry and correction; pass applicable admission checks before activation. GAP-01; PRED-01/02/08. |
| P2-F1.3 — Prediction lifecycle and selective comparison | Pre-outcome forecast pinning, horizon, outcome correlation, idempotent resolution and relevant-signal comparison; optional next-act review, not speculative inference/prefetch. | Hit/miss/inconclusive, absent outcome, pre-outcome pin, topic switch, surprise versus contradiction, bounded reappraisal and missed-signal fixtures. GAP-01/02; PRED-03/04/05/07. |
| P2-F1.4 — Subjective projection and conversational intentions | Existing Context Builder composes eligible beliefs/expectations, relationships, context and condition/expiry/relevance-aware intentions separate from user forecasts. | Bounded projection, stale suppression, interruption/resumption, explicit instruction precedence; no new model store, automatic callback or forced agenda. GAP-05/10; 037/041/106, PRED-09; DEC-04 separate. |
| P2-F1.5 — MetaMemory and experience significance | Review 05D.5 and 07B2 gap/feedback eligibility; address only evidenced remaining knowledge-gap lifecycle, clarification and experience significance. | Unknown/ambiguous/stale versus resolved gaps, delivered-choice suppression, source-linked experiences through compression; no unapproved retention changes. GAP-04/12. |
| P2-F1.6 — Integrated cognitive acceptance | Cross-session cognitive/expectation lifecycle regression; review post-session experience, unresolved friction, corrections and goal alignment using existing owner findings. | Recovery, scope isolation, no self-corroboration or stale projection; missing feedback is not negative feedback. GAP-20 and GAP-13 review (also P2-F2/P6-F2); no automatic session-end model pass. |

**Predictive-learning admission (P2-F1.1):** build on merged 07B2 [cognitive lineage](mvp-07b2-cognitive-lineage.md) (#135) and [derived projection eligibility](mvp-07b2-derived-projections.md) (#138), plus applicable response (#132), summary (#133), raw foreground (#136) and Task/Procedure (#137) lineage. Establish the [predictive admission contract](predictive-user-modeling-refinement.md#61-predictive-learning-admission-contract-p2-f11) before persistent real-user learning. P2-F1.1 may define shared contracts and synthetic fixtures without a finished predictor; actual expectation, forecast, projection and learned-approach paths must pass applicable checks before activation in P2-F1.2–4/P2-F2. Unsupported sources remain ineligible. Full 07B3/US-085 is not an entry dependency, but applicable access, consent, correction, revocation and reset controls are.

- P2-F2: distinguish immediate persona reaction from persistent mood, preserve uncertain revisable user-affect observations and Relationship preferences; use existing compact state until a representation decision is approved. Avoid unsupported mirroring, attachment or certainty. Apply contextual surprise without character judgments, and reversible approach learning only after the P2-F1.1 admission checks plus actual delivered-approach correlation and unknown-attribution safeguards (PRED-05/06/10).
- P2-F3: retain separate conversation, cognition and task attention; rerun the long-conversation compression/restart and concurrent-owner tests in [P2-08](phase2-08.md). Review actual expression and selective retrieval against cross-session cases. Neither self-repeated hypotheses nor subjective appraisals establish factual corroboration. Measure [relevance-gate evaluation](predictive-user-modeling-refinement.md#62-relevance-gate-and-measured-acceptance-p2-f3) on independently labeled held-out cases: precision/recall, missed eligible immediate appraisals, deferred recovery, unnecessary retrieval, and added interactive p50/p95 latency. Report denominators, observation horizon and abstention reasons; agree thresholds from baseline before acceptance. No mandatory extra model pass; optional routing never bypasses mandatory correction/authority checks.
- Exit: §14's compression/restart and independent concurrent-update demonstration plus confirmed backlog state/correction criteria, evidence-qualified natural expression and no stale or unauthorized owner writes. Depend on MVP-05 and the applicable MVP-07B lineage/correction contracts. The proposed AffectSchema and subjective-moment mechanisms remain DEC-04.

### MVP-06 — Natural-language goals and procedural continuity

**Capability-gap review checkpoints (not committed implementation):** GAP-01, GAP-03, GAP-10, GAP-11, GAP-16 — see [§9](#9-capability-gap-review-register).

Primary IDs: 046, 047, 048, 051, 100, 049, 050, 052, 053, 054, 055, 056, 057, 058, 098, 099.

| Increment | Deliverable | Demonstrable check |
|---|---|---|
| 06A — Approval-pending goals | Route a genuinely requested goal into a scoped candidate; bind a separate approval turn to exact objective, success criterion and version; use existing Goal command gates. | Casual desire, hypothetical or changed objective is not accepted automatically; explicit unambiguous approval creates one goal; rejection, stale reply, duplicate approval and switch cannot create unauthorized goals. |
| 06B — Guided procedures | Improve repeat, next, back, clarify, skip, pause and resume via existing Dialogue/ProcedureRun contracts; only valid method/step transitions admitted. | Nginx certificate fixture resumes after interruption/restart at first unresolved step; no instruction treated as completed command; uncertain report and failure handled without unsupported step completion. STABLE method is not silently optimized. |
| 06C — Bounded decomposition and evidence | Complete general delegated goal contribution review, evidence-qualified task outcomes, recurring progress and priority checks; preserve user versus agent executor. | Approved diet/exercise goal yields bounded verifiable supporting tasks; thinking aloud does not change approved target; corrections revoke stale task authority; user-reported DONE is not independently verified. |
| 06D — Strategy discovery and adaptation | Add a minimal authorized read-only research route with source-linked strategy proposals; compare alternatives and preserve target; evaluate failure before proposing private adaptation. | Approved research returns cited strategy evidence; unsupported or unavailable source stays uncertain; no unapproved tool or goal mutation; parameterized/adaptive variation validated, stable method unchanged; user-approved method adoption distinct from observed success. |

06A — [Approval-pending goal dialogue](mvp-06a-goal-conversation.md): separate Goal-owned proposal revisions, bounded explicit user goal requests/criterion refinement, separate adjacent delivered confirmation and rejection, atomic existing Goal creation contract and receipt. No model authority, tasks or operations before approval. PR #127 merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45806428624) passed (2,182 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45806428618) passed. US-098 remains P pending generalized goal interpretation, related-goal links and acceptance.

06B — [Guided procedure dialogue](mvp-06b-guided-procedures.md): bounded named or delivered-context navigation over existing approved runs; actual delivery binds step/run versions, explicit user attempt/report and separate confirmation reuse owner receipt/evidence contracts, pause/resume/cancel require delivered confirmation, back/repeat/skip preserve required-step eligibility. Synthetic Nginx STABLE fixture tests interruption, cross-session recovery, failure, report renewal, method pinning and no automatic execution. PR #128 merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45812760570) passed (2,226 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45812760687) passed after Ollama timeout rerun. US-100 stays P pending broader language, optional-step semantics and behavioral acceptance.

06C — [Bounded decomposition and recurring evidence](mvp-06c-goal-cycles.md): normalize exact 06A frequency pairs for existing count contribution checks; stop autonomous expansion at existing work; one approved USER occurrence task at a time for supported positive recurring count goals, explicit UTC cycle and immutable slot binding. Read current report/confirmation evidence with correction and prerequisite invalidation; no automatic completion, no child-count rollup or dispatch. PR #129 merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45822820464) passed (2,253 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45822820516) passed. Includes ancestor-window review correction. US-047/057/058 remain P; broad goal semantics and natural-language task approval remain open.

06D — [Authorized source research and diagnostic adaptation](mvp-06d-strategy-research.md): explicit approved read-only saved-document query for an approved goal, source-pin candidate and separate selection, recheck current source eligibility, bounded comparison of pinned personal run assessments, diagnosis-first parameter/private adaptation guidance. Existing Procedure owner controls proposed/applied private revisions; STABLE unchanged. PR #130 merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45825422385) passed (2,261 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45825422330) passed. Local research slice verified; external registered Task/Operation provider still missing. US-050 remains M pending broader implementation assessment; do not claim Phase 3 exit.

Predictive refinement integration: reuse 06C owner-qualified recurring task outcomes and 06D support/strategy evidence for PRED-01/06/07; targeted follow-ups only when the P2-F contracts require them. No reopening of merged slices, automatic goal changes or change to the existing sequence.

Reuse [Goal authorization](mvp-03a1-delegated-goal-authorization.md), [decomposition](mvp-03a2-general-subgoal-decomposition.md), [procedure library](phase3-03.md), [procedure runs](phase3-04.md), [dialogue continuity](mvp-03b2-procedure-dialogue-continuity.md), [adaptation](phase3-05.md), [integrated Phase 3 gate](phase3-06.md). The existing direct-goal API already requires separate exact approval; do not bypass it. Goal owns objectives/strategies, Task actionability and executor, Procedure definitions/runs, P0 operations. General strategy research requires an authorized registered provider; defer implementation until that boundary is defined, not a new generic autonomous browser. Phase 3 exit requires the Phase 2 gate and §14's verifiable task decomposition and method adaptation while preserving the objective.

### MVP-07A — Authenticated application boundary

**Capability-gap review checkpoints (not committed implementation):** GAP-15, GAP-17 — see [§9](#9-capability-gap-review-register).

Primary IDs: 004, 009, 010.

The existing [acceptance host](../src/persona_agent/acceptance_host.py) is a trusted local operator JSONL harness, not a production identity boundary. The [integrated acceptance document](phase1-integrated-acceptance.md) explicitly prohibits remote unauthenticated exposure.

- 07A1: choose the smallest deployment/client boundary; document actor binding, session identity, selected persona, authenticated presence and trusted platform callbacks. Reuse public owner contracts and local test harness; never trust client-supplied user/persona authority or tool payload trust.
- 07A2: implement one authenticated text ingress/output path with durable response correlation and reconnect/replay. Transport ACK and generation fences must be checked at delivery; external effect and permission changes remain versioned and owner-authorized. Deny unauthorized origin/capability/tenant by default.
- 07A3: validate provider-neutral adapter contracts for the actual chosen deployment (confirmed portion of 010). Keep optional model-selection descriptors, native tool calling and token streaming behind DEC-02. Existing Ollama requests are nonstreaming; do not claim token streaming exists. If explicitly approved, a follow-up text-streaming slice must distinguish provisional display from committed state, prevent premature work claims, discard stale streams, handle backpressure/cancellation and ACK only actual accepted delivery.
- Exit: two authenticated users cannot read or mutate one another; forged source identity and stale selection denied; replay deduplicated; reconnect restores authorized response/operation status; explicit revocation during work prevents new dispatch or stale commits. No external exposure before this gate and applicable MVP-07B controls. The initial architecture MVP's local responsive-core gate is distinct from deployment readiness.

07A merged — [Authenticated local text application](mvp-07a-authenticated-application.md): operator-provisioned expiring/revocable scoped credentials, strict JSONL message/resume/status/ACK/presence allowlist, current selection/session commit fences and offered-response correlation. Registered immediate reads only; asynchronous operation admission, worker callbacks, remote networking and arbitrary provider configuration denied. PR #131 merged; [technical CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45829385892) passed (2,278 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45829385876) passed. US-004/009/010 remain P. Real external deployment and generalized authorization/revocation remain gated by applicable 07B and additional 07A work.

### MVP-07B — Data lifecycle and evidence provenance

**Capability-gap review checkpoints (not committed implementation):** GAP-12, GAP-17, GAP-19 — see [§9](#9-capability-gap-review-register).

Primary IDs: 117, 118, 085.

- 07B1: inventory persisted private records, evidence and derived stores, logs, artifacts, backups and provider routes; agree actual deployment, retention/consent/access/erasure requirements and operator responsibilities. Distinguish mandatory scoped confidentiality/correction from additional policy proposals (DEC-03). Do not declare legal compliance or assume retention durations.
- 07B2: establish owner-specific lineage and revision invalidation contract for Memory and derived consumers; reuse existing source IDs, revisions, events, outbox and operation fences. Source trust and verification survive derivation; correction, expiry or revocation invalidates relevant derived views and eligibility. Do not convert interpretation into verified facts.
- 07B3: implement authorized correction/deletion propagation across the actual scoped data inventory; account for backups and external copies with explicit capabilities and documented limits. Test access denial, reindex/retrieval behavior, late worker results and replay. Audit without retaining deleted private content.
- Exit: correction or revocation does not reappear via a stale derived context or in-flight worker; cross-scope deletion denied; agreed retention/deletion and recovery procedures evidenced. Baseline safeguards and deployment requirements precede deployment; complete end-to-end privacy propagation remains Phase 6 acceptance work, with Phase 2 dependencies handled early.


07B1 — [Initial deployment/data inventory](mvp-07b-data-lifecycle-inventory.md) records owner groups, derived copies, local provider/artifact routes, open operator policy and priority invalidation work. This initial inventory merged with PR #131; actual policy decisions and per-field inventory remain open. No erasure or compliance claim.

07B2 first increment — [Persistent response lineage](mvp-07b2-response-lineage.md): immutable response read-fence metadata, historical revision/expiry and exact preference dependency checks at replay, text offers/ACK and speech admission. Guarded assistant text is not copied into new derived conversation, summary or cognition inputs; invalidated generated replies are not a worker readiness barrier. Merged in PR #132 (`2ca8985`); final head `1c81164` passed [PostgreSQL contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45835070197) (2,287 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45835070154). Standalone contracts without Psycopg passed. This is a conservative first vertical slice.

07B2 second increment — [Summary source lineage](mvp-07b2-summary-lineage.md): explicit bounded source-turn set, omission of exact Memory-invalidated inputs, late summary commit revalidation, and whole-range eligibility for summaries and their episodes. Merged in PR #133 (`f229600`); final head `c754bd3` passed [PostgreSQL contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45841164964) (2,291 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45841164697) after a model timeout rerun.

07B2 third increment — [Cognitive source lineage](mvp-07b2-cognitive-lineage.md): bounded raw and episode snapshot dependencies, late cognitive result fencing, eligible reflection and intention reads, and response-lineage protection for selected goal hypotheses. Merged in PR #135 (`7e18dfe`); final head `cad404f` passed [PostgreSQL contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45866145844) (2,298 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45866145718).

07B2 fourth increment — [Raw foreground lineage](mvp-07b2-raw-foreground-lineage.md): preserve chronological omission markers, exact bounded response source sets, inherited raw-only assistant dependencies, last-boundary validation and range-safe async assistant projection. Merged in PR #136 (`27a950d`); final head `a58553c` passed [PostgreSQL contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45882904720) (2,307 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45882904716).

07B2 fifth increment — [Task and Procedure evidence lineage](mvp-07b2-workflow-evidence.md): exact Task report/confirmation eligibility, pinned private method/step/evaluation provenance, AGENT grant and prerequisite confirmation pins, workflow context and adaptation gates. Merged in PR #137 (`66b888e`); final head `2e00d4a` passed [PostgreSQL contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45889760151) (2,316 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45889760150).

07B2 sixth increment — [Derived retrieval and projection eligibility](mvp-07b2-derived-projections.md): complete pattern input dependencies, ordinary compacted-summary response fence, current MetaMemory gaps, and document/association/feedback eligibility. Existing PostgreSQL FTS owner joins remain authoritative; no vector index or persistent retrieval cache introduced. Merged in [PR #138](https://github.com/BetrayedByEthanol/Chatbot/pull/138) (`827073d`); final head `585463640a289fdd3c7046300d207fc9348de58f` passed [Phase 0 contracts](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45894540960) and [Phase 1 live integrated acceptance](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45894541044). No separate merge-commit CI verified. Dialogue/intent metadata, affect and other lifecycle projections require further inventory before US-117/118 acceptance; predictive records require P2-F1.1 admission. 07B3 and deployment remain gated.

### P4-F — Bounded Background autonomy

**Capability-gap review checkpoints (not committed implementation):** GAP-11, GAP-18 — see [§9](#9-capability-gap-review-register).

Primary IDs: 062, 063, 066, 067, 064, 065, 068, 069, 070, 101, 102, 103.

Build on [ALIGN-01](phase4-03-align01.md), [Phase 4 integrated acceptance](phase4-07-integrated-acceptance.md) and [notifications](phase4-06-notifications.md). Do not replace the coordinator or bypass the strict preparation barrier.

- P4-F1: complete reactivation mailbox/critical-state restoration and relevant notifications (explicit opt-in, timezone, quiet hours, in-app correlation). Inactive persona must not run cognition or regain authority from a notification.
- P4-F2: run real registered workload contention/preemption tests, including foreground return, switch, resource reservation, independent user-requested work, suspension/resumption and bounded recovery. Existing recovery counts are provisional policy; no new retries established.
- P4-F3: confirmed bounded-permission capability development (068) only after agreeing sandbox, artifact review, least privilege, tool registration and reconciliation contracts. No autonomous installation or unrestricted code execution; CAP-OPP discovery remains DEC-05 and does not grant permission.
- Exit: selected absent user progresses authorized work only after current epoch preparation/ALIGN-01; return stops new autonomous work and fences late effects; inactive persona cannot run; approved notifications obey platform policy; unknown effects quarantined, terminal failures visible. Verify §14 gate and target hardware budget; don't claim stub tests measure natural cognitive quality.

### P5-F — Voice and social channels

**Capability-gap review checkpoints (not committed implementation):** GAP-14, GAP-15, GAP-17, GAP-18 — see [§9](#9-capability-gap-review-register).

Primary IDs: 073, 074, 075, 076, 077, 119, 116.

- P5-F1: integrate authenticated media host using [streaming speech input](phase5-02-streaming-speech-input.md), [speech output](phase5-03-streaming-speech-output.md) and [turn-taking](phase5-04-natural-turn-taking.md). Test actual mic/speaker and hardware AEC before enabling full duplex. Default blocked mic is safe, not true barge-in; committed text synthesis is not token streaming.
- P5-F2: bind Discord/Twitch participant/channel identity through existing ingress, then arbitrate concurrent inputs without selecting a persona or confusing private primary-user context. Confirm participant isolation before opening channels; decide extra retention policy separately.
- P5-F3: coordinate expression/avatar with the same authorized, bounded persona affect projection and response lifecycle. No separate conversational agent or untrusted provider authority.
- Exit: real concurrent input/interruptions do not mix sources, lose independent tasks or synthesize obsolete replies. Per-clip generated/queued/acknowledged playback are distinct; ACK is not proof of human comprehension. Test fallback and echo behavior only within confirmed contracts or explicitly adopted refinements. Live hardware/provider and third-party privacy evidence required.

### P6-F — Evaluation and final hardening

**Capability-gap review checkpoints (not committed implementation):** GAP-01–03, GAP-07–09, GAP-12, GAP-13, GAP-18–20 — see [§9](#9-capability-gap-review-register).

Primary IDs: 081, 082, 083, 084, 086, 087, 107.

- P6-F1: specify regeneration of an earlier response with immutable history and explicit causal fencing; define what is superseded versus retained before implementing. Never replay or undo external effects implicitly.
- P6-F2: complete evidence-based domain evaluation, prediction calibration and periodic drift/identity/sycophancy review; extend the minimal P2-F1 prediction resolution lifecycle and P2-F2 attribution safeguards with independent outcome/approach effectiveness assessment ([PRED-ROADMAP-01](predictive-user-modeling-refinement.md)). Predictions are not user facts; later independent evidence determines outcome. No generic evaluator may mutate owner state directly.
- P6-F3: complete reviewed shared-procedure improvement promotion using evidence without leaking private variant history; preserve stable methods, parameter scope and authorized publication.
- P6-F4: integrate replay/fault/permission and privacy recovery drills and measured latency/cost/quality gates; reuse early MVP-04 evaluation and MVP-07B propagation evidence. New model or hardware variants need traceable acceptance; adjudicated human findings are distinct from contract CI.
- Exit: §14 recovery, permission, goal/procedure and performance gates demonstrated; confirmed stories reviewed criterion by criterion, remaining risks recorded and release decision explicit. No benchmark or phase is automatically accepted by a merged document.

## 5. Proposal decision register

All 32 proposed-only/deferred stories are tracked below. Review individually against the v2.5.1 companion sources; do not adopt a whole proposal family through a planning merge. Existing compatible behavior can stay without adopting a new vocabulary or schema. Mixed-authority story details remain unresolved unless explicitly decided.

| Decision | IDs (PPA-US-) | Required decision and earliest appropriate work |
|---|---|---|
| DEC-01 — Contract reconciliation | 013, 097, 110, 122, 123, 124 | Determine whether to adopt CRA-01–14, proposed confidence semantics, helper/namespace and reference-flow changes. Require compatibility mapping; Phase 0–1 if approved. |
| DEC-02 — Streaming and adaptive routing | 014, 025, 026, 112, 096, 108 | Adopt individual MODEL/OUT/APS/CRA proposals only if beneficial; define guarded streaming, native tool output, sparse hints, model eligibility and consequential review boundaries. Before the relevant feature, not a global redesign. Existing admission-before-acknowledgment retained. |
| DEC-03 — Deployment privacy | 015, 120, 121 | Decide consent/retention, provider sensitive-data routes, jurisdiction/deployment review and external copies for actual deployment. Early MVP-07B policy work; do not defer mandatory base protections. |
| DEC-04 — Affect and subjective cognition | 042, 043, 044, 045, 095 | Decide compact versus eight independent dimensions, calibration, decay/retention, subjective moments and prediction callbacks; Phase 2 or later. Current compact representation provisional. |
| DEC-05 — Capability opportunities and notes | 059, 060, 061, 109, 089 | Decide CAP-OPP-01, lightweight alternatives, authorization, outcome metrics and separate procedure-note proposal; no earlier than Phase 3 for opportunity discovery if adopted; outcome evaluation after actual use. No autonomous registration authority. |
| DEC-06 — Background optimization | 071, 072, 114 | Decide cross-epoch preparation reuse, uncertain revision review and cost/admission metrics after safe baseline and confirmed ALIGN-01. Phase 4. |
| DEC-07 — Multimodal refinements | 078, 079, 080, 115 | Decide observation freshness, provider normalization, authorized fallback/self-echo and interruption cadence individually; Phase 5. Never use an echo technique as authentication. |

Other open decisions inherited from the backlog: exact thresholds for semantic review and consequence categories; numeric retry/timeout/escalation policies; target models/quantization/hardware and p50/p95 latency/quality/cost budgets; applicable privacy policy; approval of proposed detail within 010, 024, 068, 085, 088, 102, 113, 117, 118 and 119. This roadmap does not settle them.

## 6. Phase exits and PR process

Architecture §14 exit gates remain the source of truth:

| Phase | Demonstrate before declaring the phase accepted |
|---|---|
| 0 | Switch isolation, command idempotency, event replay, unauthorized-action denial and competing worker claims pass. |
| 1 | Immediate small-tool answer, slow tool result without blocking new turns, restart restores operation correlation. Confirmed backlog conversational intent, instruction persistence, trusted time and natural concise presentation also reviewed; deployment gate separate. |
| 2 | Long conversation survives compression/restart; independent cognitive updates do not overwrite each other. Confirmed memory, entity, affect and provenance coverage reviewed. |
| 3 | Recursive plan reaches verifiable tasks; goal review preserves main objective and adapts failing method appropriately. |
| 4 | Selected absent user progresses authorized work; return suspends; inactive persona never runs cognition; notifications follow platform policy. |
| 5 | Concurrent inputs and interruptions handled without source confusion or lost independent tasks. |
| 6 | Recovery drills, permission audits, goal/procedure scenarios and measured latency/cost thresholds pass. |

**PR acceptance template:** affected canonical IDs and specific source criteria; current baseline SHA; owner API/contract and invariants; smallest change and migration implications; automated test commands with observed result/CI run link; separate target-model/human/hardware evidence where needed; known limitations and unresolved decisions; updated matrix rows. Record "not run" when applicable. Avoid duplicate acceptance criteria in the roadmap: keep full requirements in the master backlog and per-story evidence in the matrix. Create a distinct MVP acceptance checklist after review of this roadmap, using the confirmed Phase 0–1 criteria and explicit deployment scope.

## 7. Complete primary story-to-work-package crosswalk

I/P/M counts below are copied from the merged matrix; neither a story's primary track nor a planned increment changes its authority or current status. Every story appears exactly once in this primary allocation. This is a proposed planning choice, not a reclassification of the architecture phase.

| Track | Stories | I / P / M | Canonical IDs (PPA-US-) |
|---|---:|---|---|
| BASE — Foundation regression | 9 | 9 / 0 / 0 | 001, 002, 003, 005, 006, 007, 008, 011, 111 |
| MVP-04 — Conversational quality | 14 | 4 / 10 / 0 | 012, 104, 016, 017, 018, 019, 020, 021, 022, 023, 024, 090, 105, 088 |
| MVP-05 — Time, recall and entities | 12 | 7 / 5 / 0 | 091, 113, 029, 030, 031, 034, 093, 027, 028, 032, 033, 092 |
| MVP-06 — Goal and procedure dialogue | 16 | 10 / 5 / 1 | 046, 047, 048, 051, 100, 049, 050, 052, 053, 054, 055, 056, 057, 058, 098, 099 |
| MVP-07A — Authenticated application | 3 | 0 / 3 / 0 | 004, 009, 010 |
| MVP-07B — Data lifecycle and provenance | 3 | 0 / 2 / 1 | 117, 118, 085 |
| P2-F — Remaining cognitive state | 9 | 4 / 5 / 0 | 036, 106, 035, 037, 038, 039, 040, 041, 094 |
| P4-F — Background autonomy | 12 | 9 / 2 / 1 | 062, 063, 066, 067, 064, 065, 068, 069, 070, 101, 102, 103 |
| P5-F — Voice and social | 7 | 1 / 3 / 3 | 073, 074, 075, 076, 077, 119, 116 |
| P6-F — Evaluation and hardening | 7 | 1 / 3 / 3 | 081, 082, 083, 084, 086, 087, 107 |
| DEC-01 — Contract reconciliation | 6 | 0 / 5 / 1 | 013, 097, 110, 122, 123, 124 |
| DEC-02 — Guarded streaming and model routing | 6 | 1 / 5 / 0 | 014, 025, 026, 112, 096, 108 |
| DEC-03 — Deployment privacy proposals | 3 | 0 / 0 / 3 | 015, 120, 121 |
| DEC-04 — Affect and subjective cognition | 5 | 1 / 3 / 1 | 042, 043, 044, 045, 095 |
| DEC-05 — Capability discovery and procedure notes | 5 | 0 / 1 / 4 | 059, 060, 061, 109, 089 |
| DEC-06 — Background optimization | 3 | 0 / 3 / 0 | 071, 072, 114 |
| DEC-07 — Multimodal refinements | 4 | 1 / 2 / 1 | 078, 079, 080, 115 |

**Coverage:** 124 unique canonical IDs: 92 confirmed/mixed in implementation and verification tracks; 32 proposed-only/deferred in decision tracks. No confirmed story is intentionally omitted. See [matrix](user-story-implementation-matrix.md) for original phase, priority, owner, individual references and limitations.

## 8. Maintenance

At each merged development PR update the affected matrix rows and milestone evidence, not historical counts. Replan only on reproduced defects, dependency changes, explicit proposal adoption or user direction. Keep the seven phases and MVP exclusions unchanged unless the architecture is explicitly amended. Preserve private review artifacts outside the public repository. This document records planning, not execution; no new runtime tests or benchmarks were performed to create it.

## 9. Capability gap review register

**Added 24 November 2026**, review baseline `d1ceda0036914a70eaffce7f0628808f965087d2` (PR #105 merged). Sources: predictive-user-model and broader feature-coverage reviews against Architecture v2.5.1, Appendix A baseline features, legacy [README](../README.md) and [systems list](../systems.md), BEH, MODEL/EMO/OUT, APS, CAP-OPP, CRA and the reconciled master backlog. This is a requirements/traceability review, not a fresh exhaustive implementation audit.

Track overlooked or underspecified capabilities when their dependencies are being developed. GAP identifiers are stable planning references, not canonical PPA-US IDs, new architecture phases, status promotions or approval of companion proposals. Existing confirmed requirements remain binding; proposed details, placement and candidate checks await review. Bare three-digit source IDs below refer to PPA-US stories; BASE-FEAT refers to Architecture Appendix A. See the [matrix](user-story-implementation-matrix.md) for actual authority and evidence.

### 9.1 Disposition policy

- At the first applicable milestone planning/review, inspect existing code and tests against the source requirements. Record a disposition before closing that planning review; implementation of every gap is not a prerequisite to completing an existing increment.
- **OPEN:** awaiting review. **COVERED:** existing evidence satisfies gap. **ADOPT:** approved work linked to story criteria and an increment. **DEFER:** retain item, record reason and future review milestone. **REJECT:** decline proposed extension with reason. Record date, decision reference, owner, target/review milestone and evidence or limitation. Deferred items remain visible, not implemented.
- Existing confirmed obligations cannot be waived by this register. COVERED needs evidence; DEFER may reschedule work but not bypass required phase/release criteria. REJECT applies only to a proposed extension unless architecture is explicitly amended. Mixed-authority details and companion proposals remain unapproved.
- Keep the 124-story crosswalk, Phase 0–1 initial MVP and next increment MVP-04A unchanged. On adoption, extend existing story criteria where appropriate through an explicit backlog decision; create new canonical IDs only if necessary. Update the matrix and checklist when scope or evidence changes.
- Preserve existing domain ownership and bounded asynchronous work; no new scheduler, mandatory per-turn inference, standalone authoritative user/world model, or uncontrolled optimization. Predictions are private scoped interpretations, subject to source correction, expiry, revocation and retention. They cannot grant authority, override current explicit instructions or become verified facts.
- User-specific learning requires comparable independent observations accumulated over time. Use persona defaults and explicit preferences at cold start; record missing outcomes as unknown. No sample-size, calibration, decay or training algorithm is fixed here. Begin with approved synthetic cases/evidence hygiene where useful.

### 9.2 Tracked gaps and checkpoints

Items initially OPEN; current dispositions and decision history below supersede that initial state. Review timing is a proposed opportunity, not an automatic implementation requirement. Candidate checks distinguish behavior; acceptance remains subject to the owning domain and original requirements.

| Gap ID / capability | Review milestone(s) | Source / related stories | Candidate review scope and checks | Disposition |
|---|---|---|---|---|
| GAP-01 — Predictive user modeling and contextual surprise | P2-F1.1–3/F2 after 05B–D and applicable 07B2; 06C outcomes; P6-F2 calibration | ARCH §§19.1, 20.1–20.4, 22.2; 033, 036, 038, 039, 083, 106; proposed 044 | Scoped behavioral expectations, horizon, original evidence, comparable sample sufficiency and revision history. Compare expectations fixed before events to later observations. Test two different task-completion histories, cold start, changing habits and unknown outcomes; distinguish surprise from contradiction. No global reliability judgment or mandatory model pass. | ADOPT — scoped PRED-ROADMAP-01 |
| GAP-02 — Next-intent prediction and context prefetch | 04C evidence; P2-F1.3 review after 05B/C; P2-F3 measurement | BASE-FEAT-CON-07; 017, 018, 027, 090, 113, 083 | Compare prior anticipated next act to actual input. Optional scoped prefetch outside critical path; test hits, misses, topic switch, stale authority and cost. Current input wins. | OPEN |
| GAP-03 — Individual conversational approach suitability | 04B feedback review; P2-F2 learning; 06D support; P6-F2 evaluation | ARCH §§20.2, 20.4, 21.2, 22.1–22.3; 024, 040, 082, 106; BEH R02/R03/R06 | Learn tentative context-specific suitability of brevity, humor, directness, encouragement, initiative and questions from repeated user-specific feedback. Cold start uses persona defaults and explicit preferences. Attribute feedback to delivered approach; distinguish approval, confusion, discomfort, outcomes and mere engagement. Test changing preferences and opt-out. No covert experiments or engagement maximization. | ADOPT — scoped PRED-ROADMAP-01 |
| GAP-04 — MetaMemory and knowledge-gap lifecycle | 05D after 05B/C; P2-F1.5 follow-up | ARCH §§5, 19.1–19.3; BASE-FEAT-COG-02/MEM-05; 030, 034, 037, 092 | Track relevant unknown, ambiguous, contradictory, stale, awaiting clarification and unverifiable knowledge with evidence and resolution history. Test timely clarification, correction and repeated-query suppression; absence is not evidence of a fact. | OPEN |
| GAP-05 — Subjective world model and belief projection | P2-F1.4 after 05B–D | ARCH §§20.1, 20.4; 036, 106, 093; proposed 043 / BEH R01 | Compose relevant facts, relationships, observations and competing interpretations into a read-only subjective context. Test revision across sessions and context changes, preserving uncertainty and role boundaries; no independent world-model authority or universal graph. | OPEN |
| GAP-06 — Cognitive continuity across reflection ranges | P2-F1.1/1.6 before Phase 2 exit review | ARCH §20.1; 035, 036, 037, 106 | Revisit unresolved questions and prior hypotheses when new evidence arrives; link competing, superseded and supported interpretations. Test restart, interruption and correction across ranges. No endless reflection or self-corroboration. | OPEN |
| GAP-07 — Conditional input, claim and output verification | 04B/04C; 05D sources; P6-F4 regression | BASE-FEAT-COG-08/OUT-04; ARCH §§11.3, 20.3–20.4; 105, 113, 117, 086 | Inspect existing grounding and claim enforcement. Test unsupported consequential claims, factual conflicts, stale sources and detectable output inconsistencies. Verify selectively through authorized sources or clarify/qualify; no unconditional verifier pass. | OPEN |
| GAP-08 — Conversational repair and misunderstanding detection | 04C; 05B/C continuity; P6-F2 evaluate | ARCH §§18.1–18.2, 20.3; 017, 019, 027, 090, 105, 082 | Repair affected pending act, referent or interpretation from explicit corrections; consider repeated failed explanations without treating repetition as proof of dissatisfaction. Test clear versus ambiguous repair and stale dependent work; no silent goal mutation. | OPEN |
| GAP-09 — Memory retrieval effectiveness feedback | 05B/05D; P6-F4 evaluate | BASE-FEAT-MEM-08; ARCH §§19.2–19.3, 22.1; 018, 031, 092, 113, 082 | Record retrieval misses, irrelevant hits and corrections against evidence. Test ranking/eligibility improvements on held-out cases; popularity cannot make a retrieved claim true. | OPEN |
| GAP-10 — Conversational direction and next-action planning | P2-F1.4 after 04C; 06B guided dialogue | ARCH §§4.4, 18.2, 20.1; legacy Plan_Conversational_Direction / Plan_next_action; 027, 037, 041, 100 | Bounded next conversational acts, unresolved questions, expiry and relevance separate from goals/tasks. Test topic switch, interruption and return; no forced agenda or overriding current direct requests. | OPEN |
| GAP-11 — Competing approved goal and value arbitration | 06C owner contracts; 06D alternatives; P4-F2 admission | BASE-FEAT-GOAL-04; ARCH §§6.1, 21.2; 046, 047, 051, 058 | Distinguish dependency/resource conflicts from incompatible approved outcomes. Test conflicting goals/deadlines and ask for priority when material; preserve objectives, continue unrelated authorized work. | OPEN |
| GAP-12 — Experience significance and selective retention | 05D; P2-F1.5 and 07B policy; P6-F4 | ARCH §§5, 19.1–19.3; 029, 033, 034, 118, 085; proposed 043 / BEH R05 | Assess episodic significance and salience, distinguish attention decay from deletion. Test ordinary versus significant events, correction and revocation; preserve facts/instructions independently of summaries. Deletion governed by approved policy. | OPEN |
| GAP-13 — Learning from agent mistakes | 04A/B collect examples; P2-F1/F2 adjustments; P6-F2 evaluate | ARCH §§20.1, 22.1–22.3; BASE-FEAT-COG-03; 082, 086, 106, 107 | Link specific agent error to correction or independent outcome; propose bounded, revisable owner-approved behavioral adjustment. Test recurrence reduction on novel cases and rollback. No automatic prompt/weight mutation or self-rating as ground truth. | OPEN |
| GAP-14 — Intentional silence and response obligation | 04C/D; P5-F1 turn-taking; DEC-02/07 | BASE-FEAT-CON-04; ARCH §§4.4, 18.4; 016, 022, 037; proposed 110, 115 | Distinguish intentional no-output from failed delivery or missing required response. Test optional observations, direct questions, quiet preferences and delayed results; no silent loss of user requests. | OPEN |
| GAP-15 — Owner/moderator priority interruption | 07A authority; P5-F2 channels; BASE cancellation | BASE-FEAT-CON-08/SAFE-02; 002, 004, 009, 066, 075, 076 | Define scoped authenticated interruption distinct from task cancellation or persona switching. Test forged moderator requests, stale authority, valid stop and unrelated task survival. | OPEN |
| GAP-16 — Procedure composition and rollback | 06B/D after pinned definitions and runs | BASE-FEAT-PROC-04; ARCH §§6.3, 21.3–21.4; 052, 053, 054, 056, 084 | Review reusable subprocedures, typed parameters, version compatibility and definition rollback. Test pinned runs survive updates and composition failures; no implicit undo of irreversible effects. | OPEN |
| GAP-17 — Image and game-environment perception | P5-F2/F3 after 07A and applicable 07B; DEC-07 | BASE-FEAT-IN-01/TOOL-03; ARCH §23; 004, 010, 077; proposed 078 | Review image/game-event ingress, source, capture time, permissions and historical versus live observation. Test untrusted/stale evidence; perception cannot grant game mutation authority. Select justified adapters only. | OPEN |
| GAP-18 — Graceful degradation under provider failure | 04A/D text/model; DEC-02; P5-F1 audio; P6-F4 | BASE-FEAT-OPS-04; 007, 012, 021, 067, 087, 102; proposed 096, 080 | Review bounded safe retries, compatible fallbacks and visible limitations for model/tool/audio outages. Test failed generation, unsupported schema and obsolete results; preserve durable pending state, no fictitious success or unauthorized provider. | OPEN |
| GAP-19 — Controlled evaluation dataset creation and cleanup | 04A evidence hygiene; 07B policy; P6-F4 | BASE-FEAT-OPS-05; ARCH §§12, 22.3; 085, 087, 088; proposed 015, 120, 121 | Use approved synthetic or explicitly permitted data, provenance, deduplication, retention and revocation-aware removal. Separate held-out evaluation from training; no private transcript publication or implicit training. Test deletion and private artifacts. | OPEN |
| GAP-20 — Post-session experience and goal-alignment review | P2-F1.6 session boundary; P6-F2/F4 | BASE-FEAT-OPS-03; ARCH §§20.1, 22.1–22.3; 082, 086, 107 | Review meaningful sessions for unresolved friction, helpfulness, corrections and approved-goal alignment. Owner-specific evidence findings, not length or model rating alone; test missing feedback, neutral endings and explicit dissatisfaction. | OPEN |

### 9.3 Decision history

When an item is reviewed, update its disposition above and append a record below; preserve its ID and prior decisions.

| Gap ID | Decision / date | Owner | Target or next review | Rationale, evidence, affected criterion and decision reference |
|---|---|---|---|---|
| GAP-13 | OPEN / 24 Nov 2026 | Evaluation | 04B; P2-F1/F2 | MVP-04A review diagnostics preserve evidence; no learning or owner adaptation implemented. Reassess after real independent ratings and reproduced defects. |
| GAP-18 | OPEN / 24 Nov 2026 | Model Gateway / Execution | 04D; DEC-02 | Bounded ModelUnavailable transport classifications and regression cases; old failure root cause not recoverable from generic evidence. No retry or fallback authority adopted. |
| GAP-19 | OPEN / 24 Nov 2026 | Evaluation / Governance | 07B; P6-F4 | Create-only private operator status report verifies bundle/packet consistency and records legacy fingerprint limit; full dataset lifecycle policy remains open. |
| GAP-03 | OPEN / 25 Nov 2026 | Conversation / Relationship | P2-F2; 06D; P6-F2 | MVP-04B reviews casual-chat and low-effort failures and adjusts bounded guidance; no personalized learning adopted. Real-model response evidence pending. |
| GAP-07 | OPEN / 25 Nov 2026 | Conversation / Memory / Tool Gateway | 04C; 05D; P6-F4 | MVP-04B extends trusted clock routing and source-qualified personal-fact context. Guidance is not a general output verifier; unsupported claims still require behavioral tests. |
| GAP-13 | OPEN / 25 Nov 2026 | Evaluation | 04B rerun; P2-F1/F2 | Reproduced archived examples inform focused routing and prompt tests. No automated learning or accepted review; verify recurrence on held-out cases. |
| GAP-02 | OPEN / 25 Nov 2026 | Conversation | P2-F1; 05B/C | Current input cues and explicit topic switch suppress stale pending-question interpretation. No predictive prefetch added. |
| GAP-07 | OPEN / 25 Nov 2026 | Conversation / Tool Gateway | 04C follow-up; 05D; P6-F4 | Narrow current-turn reply adjustment with source-correlated audit event; clock claim safeguards only on model path. General personal-fact verifier and D&D/weather owner checks remain open. |
| GAP-08 | OPEN / 25 Nov 2026 | Conversation | 04C follow-up; 05B/C; P6-F2 | Explicit pause-and-switch and deferral preserve independent pending questions; ambiguous correction/repair still needs broader cases. |
| GAP-14 | OPEN / 25 Nov 2026 | Conversation / Communication | 04D; P5-F1 | Low-effort and no-question safeguards deliver short acknowledgments, not silent missing replies. Intentional silence not adopted. |
| GAP-07 | OPEN / 25 Nov 2026 | Conversation / Memory | 05D; P6-F4 | 04C.2 checks bounded arithmetic evidence and separates user-reported claims from owning-domain changes; no Game/Weather capability or generic personal-fact verifier introduced. |
| GAP-08 | OPEN / 25 Nov 2026 | Conversation | 05B/C; P6-F2 | 04C.2 checks polar short-reply compatibility and scoped delivered pending-act provenance. Longer free-text and broad reference repair remain open. |
| GAP-07 | OPEN / 25 Nov 2026 | Conversation / Memory | 05B/C/D; P6-F4 | 04C.3 canonical exact-slot saved personal-fact replies for three relationships; source/eligibility checks and evidence audit. Unconsolidated corrections, broader entity recall and general verification remain open. |
| GAP-18 | OPEN / 25 Nov 2026 | Tool Gateway / Conversation | DEC-02; P5-F1; P6-F4 | 04D renders terminal read-only tool failure/cancellation/unavailable notices through current relevance/delivery gates; retries remain P0-owned, invalid wait rejected before admission. No fallback provider or generic effectful retry authority. |
| GAP-14 | OPEN / 25 Nov 2026 | Conversation / Communication | P5-F1; DEC-02/07 | 04D preserves response obligation after admitted work fails; terminal notice is not intentional silence and obeys the same selected-generation/relevance gates. |
| GAP-07 | OPEN / 25 Nov 2026 | Conversation | 05B; P6-F4 | 05A records trusted receipt context and explicit zone, resolves bounded relative expressions and abstains on ambiguous DST. No general temporal claim verifier, presence inference or action authority. |
| GAP-07 | OPEN / 25 Nov 2026 | Memory / Conversation | 05B/C/D; 07B; P6-F4 | 05B.1 shares current revision eligibility across exact, lexical and hybrid recall; filters exact-source ineligible episodes and budgets correction chains atomically. General semantic conflicts and full derived lineage remain open. |
| GAP-09 | OPEN / 25 Nov 2026 | Memory / Conversation | 05B/D; P6-F2 | 05B.1 fixes read-path consistency and prioritizes eligible current Memory evidence over optional summaries/episodes; no retrieval feedback/learning policy adopted. |
| GAP-08 | OPEN / 25 Nov 2026 | Conversation / Memory | 05B/C; P6-F2 | 05B.2 uses a bounded receipt-anchored historical date window and marks competing source turns; unresolved dates do not widen the retrieval. General temporal/semantic repair remains open. |
| GAP-09 | OPEN / 25 Nov 2026 | Memory / Conversation | 05B/D; P6-F2 | 05B.2 constrains historical report retrieval by local calendar windows and source eligibility; no adaptive retrieval learning or as-of revision history. |
| GAP-07 | OPEN / 25 Nov 2026 | Memory / Conversation | 05C/D; 07B; P6-F4 | 05B.3 suppresses newer exact-slot disputes before consolidation and fences tracked historical evidence at model response commit. Broader semantic corrections, derived lineage and delivery-time consistency remain open. |
| GAP-09 | OPEN / 25 Nov 2026 | Memory / Conversation | 05D; P6-F2 | 05B.3 adds prospective report-source eligibility and a scoped optimistic historical dependency; coarse invalidation and retry overhead require measurement. |
| GAP-08 | OPEN / 25 Nov 2026 | Memory / Conversation | 05C follow-up; P6-F2 | 05C.1 preserves same-name entities, allows qualified and alias recall, and binds supported adjacent pronouns only to delivered grounded entity responses. Natural clarification follow-up and general reference repair remain open. |
| GAP-07 | OPEN / 25 Nov 2026 | Memory / Conversation | 05C/D; 07B; P6-F4 | 05C.1 canonical entity recall reads current scoped owner revisions at response commit, including model-only delivery. General identity/output verification remains open. |
| GAP-08 | OPEN / 25 Nov 2026 | Memory / Conversation | 05C follow-up; P6-F2 | 05C.2 admits bounded conversational identity reports and resolves short relation selections only against delivered, unchanged named choices. General semantic/reference repair and nonadjacent clarification remain open. |
| GAP-07 | OPEN / 25 Nov 2026 | Memory / Conversation | 05C/D; 07B; P6-F4 | 05C.2 retains owner validation and source evidence, serializes first-report name absence, and abstains on ambiguous corrections; general semantic entity interpretation and output verification remain open. |
| GAP-07 | OPEN / 25 Nov 2026 | Memory / Conversation | 05D; 07B; P6-F4 | 05C.3 uses explicit evidence bindings and versioned directed relation reports; exact-ID context and canonical unavailable fallback are fenced by current revision/expiry. General semantic verification and derived lineage remain open. |
| GAP-09 | OPEN / 25 Nov 2026 | Memory / Conversation | 05D; P6-F2 | 05C.3 selects only linked eligible history for an explicitly resolved identity; no name-based fallback, graph traversal or adaptive retrieval learning. |
| GAP-01 | ADOPT / 26 Nov 2026 — PRED-ROADMAP-01 | Cognitive / Memory / Emotion | P2-F1/F2; 06C integration; P6-F2 | Adopt PRED-01/02/04/05/07/08/09 scoped criteria; minimal pre-outcome prediction lifecycle before learning, selective comparison, owner-qualified evidence, no calibration algorithm fixed. See [criteria](predictive-user-modeling-refinement.md). No implementation or acceptance claim. |
| GAP-03 | ADOPT / 26 Nov 2026 — PRED-ROADMAP-01 | Relationship / Conversation / Evaluation | P2-F2; 06D integration; P6-F2 | Adopt PRED-06/10: delivered-approach correlation, unknown attribution and reversible evidence-supported adaptation before personalized learning. No engagement objective or covert experiments. See [criteria](predictive-user-modeling-refinement.md). |
| GAP-02 | OPEN / 26 Nov 2026 — PRED-ROADMAP-01 | Conversation / Cognitive | P2-F1 | Optional anticipated next-act contract reviewed with existing 04C evidence; speculative inference/prefetch optimization deferred pending measured benefit. Current input wins; no new mandatory Phase 1 work. |
| GAP-01/02/03 | Review clarification / 27 Nov 2026 — PRED-ROADMAP-01 | Cognitive / Conversation / Relationship / Evaluation | P2-F1.1–6, P2-F2/F3 | Owner accepted all four findings: bounded increments, predictive admission checks reusing merged 07B2, current evidence reconciliation and measured relevance gate. ADOPT scope of GAP-01/03 unchanged; GAP-02 remains OPEN; no new proposal adoption or acceptance promotion. |
| — | Pending other initial reviews | — | See §9.2 | Other gaps and DEC-01–07 proposals retain their dispositions; packages 2/3/5 remain linked review scope, not blanket adoption. |

**Coverage:** 20 capability gap review items, independent of the existing 124 canonical stories and 32 proposed-only story decisions. Grouping and review timing remain planning proposals except the explicitly adopted scope in PRED-ROADMAP-01. No runtime changes, implementation count changes, acceptance promotions or benchmark execution.

### MVP-05E gap review (26 November 2026)

Partial means evidence of a useful slice; these gaps remain OPEN in the authoritative register. No new proposal authority or mandatory phase gate is implied.

| Gap | Evidence disposition | Follow-up |
|---|---|---|
| GAP-04 | Partial: owner recall gap history and bounded delivered clarification suppression | P2-F1 proactive/cognitive gap discovery; 05E cooldown regression |
| GAP-05 | Open: no composed subjective world model | P2-F1 |
| GAP-07 | Partial: exact evidence checks and fenced current reads | P6-F4 verification evaluation; MVP-07B lineage |
| GAP-08 | Partial: delivered entity clarification and 05E unsupported-definition barrier candidate | Broader natural reference and correction repair in P2-F/06 where needed |
| GAP-09 | Partial: exact-query document relevance feedback | P6-F4 broad retrieval feedback and held-out evaluation |
| GAP-12 | Partial: source-linked episodes and independently retained facts | P2-F1 significance; MVP-07B retention; P6-F4 evaluation |
