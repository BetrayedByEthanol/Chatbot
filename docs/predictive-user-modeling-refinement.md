# Predictive user modeling and experience-driven adaptation — adopted roadmap refinement

**Decision:** PRED-ROADMAP-01, 26 November 2026. Project owner approved adoption of the 26 November predictive-user-modeling draft with targeted revisions, without changing the existing development sequence.  
**Status:** Approved planning scope and story-criteria extensions; not an architecture amendment, a runtime implementation, or acceptance evidence.  
**Baseline reviewed:** `main` at `f229600d24293d0cdc2883690e5d122b1cc827a5`. Architecture v2.5.1 and the reconciled 124-story backlog remain authoritative. The existing [roadmap](development-roadmap.md), [matrix](user-story-implementation-matrix.md) and [MVP checklist](mvp-acceptance-checklist.md) retain their milestones, phase exits, story IDs, authority and priorities.

**Review clarification (27 November 2026):** owner approved all four review findings. P2-F1 subdivisions, specific 07B2 admission dependencies and relevance-gate measurement refine existing planning scope; no phase, story authority or overall sequence change.

## 1. Purpose and model

A persona should react to the same observation differently when its own relevant experience supports different expectations. For example, two synthetic users report completing a workout; one previously completed 2 of 10 comparable commitments and the other 9 of 10. The observation may be more surprising for the first, without implying either user is unreliable. These figures illustrate history only, not a threshold or probability estimator.

| Concept | Meaning and owner |
|---|---|
| Observation | Evidence of what was reported or observed, preserving original source and verification class. Memory stores eligible evidence; Cognitive owns its reflective observation records; Goal/Task owns task outcomes. |
| Behavioral pattern | Memory-owned recurring contextual pattern, with support and counterexamples. |
| Expectation | Cognitive-owned scoped, revisable hypothesis about likely behavior, with evidence links, maturity and validity. |
| Prediction | Time-bounded forecast for a defined future act or outcome, pinned to expectation/version where applicable, recorded before outcome evidence is available. Cognitive owns lifecycle; Conversation can propose a bounded next-act hint. |
| Outcome | Later independent eligible evidence, possibly a user report rather than verified completion; no evidence or ambiguous match is inconclusive, not failure. |
| Appraisal | Cognitive classifies supported mismatch; Emotion owns immediate persona reaction and any separately justified mood change. |
| Learning | Owner-validated expectation or approach revision, supported by outcome/feedback; Evaluation measures effectiveness, never directly mutates state. |

Default scope is the private `(user_id, persona_id)` instance, not a global user profile. Explicit authenticated instructions and authorized user-global preferences retain precedence. No cross-persona sharing without authorization.

## 2. Adopted requirements and checks

PRED IDs are refinement labels only, not canonical user stories. Implementation admission remains within existing milestone reviews and owner contracts. The optional optimizations and separate companion proposals named below are not approved merely by this decision.

| ID | Requirement and observable acceptance check | Owner / existing milestone |
|---|---|---|
| PRED-01 | Persist contextual expectations as evidence-linked Cognitive hypotheses; two different comparable histories yield distinct scoped expectations, without a global reliability label. Reuse Memory patterns and their counterexamples rather than duplicate pattern storage. | Cognitive / Memory; P2-F1, 06C integration |
| PRED-02 | Assess evidence sufficiency and independence before relying on expectations. One report, repeated derivations of the same event, or model confidence alone cannot establish a pattern. Cold start works with no expectation. | Cognitive / Memory; P2-F1 |
| PRED-03 | Support optional expiring anticipated next-act records only where useful; distinguish a forecast of the user from a persona intention. Compare a prior forecast with later eligible input. Current input wins; missing forecast never blocks a response. Dedicated speculative inference and prefetch are deferred pending measured benefit; 04C is historical evidence, not reopened. | Conversation / Cognitive; P2-F1 review |
| PRED-04 | Selectively compare relevant new evidence to an applicable preexisting expectation: EXPECTED, UNEXPECTED, CONTRADICTORY or UNKNOWN. Ordinary greetings and unrelated turns need no lookup or comparison; no applicable evidence is UNKNOWN. Use §6.2 local routing and held-out checks; a deferred or omitted comparison is not an UNKNOWN comparison result. A surprising outcome need not contradict a fact or instruction. | Cognitive / Conversation; P2-F1 |
| PRED-05 | A supported mismatch can inform a bounded immediate reaction and selective later reappraisal. Deduplicate reappraisal by expectation version and evidence; require new evidence for a new reappraisal. Weak signals remain eligible for batching. No automatic mood mutation or accusation; ordinary situational surprise does not require historical prediction. | Cognitive / Emotion; P2-F1/F2 |
| PRED-06 | Learn context-specific approach suitability (brevity, humor, initiative, emotional expression, accountability, question frequency, teaching style and goal support) only from explicit preferences or repeated comparable attributable feedback. A single inferred reaction does not change policy; explicit corrections apply promptly. Changes stay small, reversible and within identity. | Relationship / Conversation; P2-F2, 06D integration |
| PRED-07 | Implement a minimal durable prediction lifecycle in Phase 2 before outcome-driven learning: pin prediction, context, target and horizon before outcome; correlate later eligible evidence, resolve once as HIT, MISS or INCONCLUSIVE, expire unresolved forecasts without penalty, and submit revisions through the owning domain. P6-F2 extends calibration and evaluation, not basic correlation. | Cognitive / Evaluation; P2-F1, P6-F2 |
| PRED-08 | Revise or reduce reliance on outdated expectations after relevant contrary evidence or age; preserve eligible revision history and context boundaries. Test changed habits, stale evidence, and no transfer to unrelated contexts. Retention and deletion supersede history preservation. | Cognitive; P2-F1, P6-F2 |
| PRED-09 | Retrieve only eligible expectations useful to the current turn inside the existing context budget. Missing or stale optional context falls back safely; no full user-model load or mandatory inference. Prefetch, if later approved, must not bypass read fences or alter correct behavior. | Context / Conversation; P2-F1/F3 |
| PRED-10 | Before personalized adaptation, correlate feedback with the actual delivered response/approach, context and later evidence; support unknown attribution and abstain on ambiguous causal claims. Never treat a reaction caused by the persona as an established user trait, feed a prediction back as independent evidence, or optimize engagement. P6-F2 adds independent evaluation of attribution and effectiveness. | Evaluation / Relationship / Conversation; P2-F2, P6-F2 |

### Maturity and contextual rules

| Stage | Permitted behavior |
|---|---|
| Cold start | Persona defaults and explicit user preferences; no unsupported prediction. |
| Early interactions | Tentative hypothesis; no expectation-based surprise. |
| Pattern formation | Repeated comparable evidence may inform low-confidence tone or questions. |
| Established pattern | Sufficient recent multi-occasion evidence may support contextual surprise and tailored support. |
| Behavioral change | Reassess, reduce confidence or revise once for new evidence; retain eligible competing/superseded interpretations. |
| Outdated pattern | Stop relying on stale evidence; history is subject to retention and revocation. |

Maturity is qualitative guidance, not a fixed score. No sample counts, numeric confidence thresholds, decay formulas, context taxonomy or calibration algorithm are chosen. Model-reported confidence is not calibrated; use documented owner evidence assessment or coarse/unknown confidence until calibration is justified (DEC-01). No covert behavioral experiments, engagement maximization, or automatic prompt/weight mutation. Positive reception, confusion, discomfort, task progress and mere engagement are different signals.

## 3. Processing and ownership

Reuse `cognitive_contracts.py`, `cognitive_engine.py`, `automatic_cognition.py`, `meta_episodic.py`, `affect_context.py`, `relationship_state.py`, existing historical read fences and owner commands. These are implementation entry points, not proof that prediction or approach learning is implemented.

1. Existing authenticated event journal and sparse interpretation record the input. Current explicit instructions, mandatory correction and authorization checks retain priority.
2. On a relevant signal selected under §6.2, the existing Context Builder retrieves a budgeted eligible projection. `PredictiveUserContext` is a logical read-only contribution to the existing contextual/subjective projection, complementary to persona-self/affect context; no independent pipeline, store or service. It contains context, maturity, applicable horizon and eligible provenance/revision refs, not free-form character judgments.
3. Compare against a previously formed expectation when meaningful within the interactive budget; do not block on new predictive inference. An optional supported reaction hint may influence expression through Emotion's existing owner boundary. Natural positive surprise is permitted; never expose a global negative judgment or treat surprise as fact.
4. Existing watermarked, bounded asynchronous cognition forms/revises expectations, records and resolves predictions, and routes validated proposals to relevant owners. No extra inference on every turn, per-signal job, or LLM/tool call under a database transaction. Selective immediate reappraisal remains available under existing consequential-correction rules.
5. Actual delivery and subsequent evidence feed the owner's evaluation, with scope, source class, correlation, revision, expiry and idempotency checks. A delivered suggestion is not proof of task completion. Evaluation findings do not grant permissions or rewrite approved goals, procedures, or stable identity.

Memory owns recurring evidence patterns, Cognitive owns reflective interpretations and forecast lifecycle, Relationship owns learned approach suitability separately from explicit preferences, Conversation owns choice of response approach, Emotion owns reactions/mood, Goal/Task owns authorized goals and outcomes, and Evaluation owns assessment findings through existing logical responsibilities. Explicit preferences are not overwritten by inferred suitability. No new engine, scheduler or authoritative user/world-model database.

## 4. Five refinement packages

| Package | Disposition and integration |
|---|---|
| 1 — Predictive user modeling | Adopt PRED-01–05, 07–09 within GAP-01. GAP-02 optional next-intent optimization remains OPEN for P2-F1 review; no speculative pass or prefetch commitment. |
| 2 — Subjective world model and cognitive continuity | Retain GAP-05/06 reviews at P2-F1; compose relevant facts, relations, observations and competing hypotheses via existing owners; revisit unresolved questions with new evidence. No separate store; subjective-moment and callback proposal decisions remain DEC-04. |
| 3 — MetaMemory and knowledge awareness | Retain GAP-04/09/12 reviews. Reuse 05D.5 recall gap history, delivered-choice suppression and evidence-bound feedback; add only missing knowledge-gap/significance behavior after inspection. |
| 4 — Conversational adaptation and self-learning | Adopt PRED-06/10 within GAP-03, with response-linked feedback safeguards before learning; keep broader GAP-13 mistake-learning review open. |
| 5 — Conditional verification and response quality | Retain GAP-07/08 reviews; reuse 04C bounded checks, 05A trusted time, 05B–E source eligibility and existing correction/repair. PRED-04/05 do not introduce a general verifier or mandatory per-turn pass. |

## 5. Story-criteria extensions

This is the repository's additive criteria record for the reconciled backlog. Apply at the indicated existing milestone with original criteria intact; conditional rows apply only within the adopted refinement or when the separately proposed mechanism is approved. No canonical story, phase priority, authority classification or implementation status is changed by this planning decision.

| PPA-US | Added criterion / trace | Review |
|---|---|---|
| 017, 018, 027, 090 | Current input overrides stale anticipated act; optional hints do not resolve dialogue or select a route by themselves (PRED-03/09; optimization remains optional). | P2-F1; 04C evidence |
| 024 | Explicit response preferences override inferred suitability; no fixed universal reply length (PRED-06). | P2-F2; 04B evidence |
| 033 | Behavioral patterns link comparable distinct eligible episodes and counterexamples, preserving evidence classes (PRED-01/02). | P2-F1; reuse 05D.2 |
| 035 | Form and revise expectations in existing bounded watermarked reflection; no per-turn job (PRED-01/02/07). | P2-F1 |
| 036 | Scoped versioned expectation has supporting observations, context, maturity and validity; reject unsupported or self-corroborating expectation (PRED-01/02/08). | P2-F1 |
| 037 | Forecast of a user's next act is not a persona intention; any optional intention has separate condition, expiry and relevance (PRED-03/05). | P2-F1 |
| 038 | Supported contextual surprise may affect immediate reaction; mood update requires separately justified appraisal (PRED-05). | P2-F2 |
| 039 | Existing user-affect uncertainty and revision remain separate; expectation drift belongs to 036/106, not Emotion (PRED-08 boundary). | P2-F2 |
| 040 | Relationship suitability changes only from explicit preference or repeated comparable attributable evidence; record provenance and reversible revision (PRED-06/10). | P2-F2 |
| 043 (proposed) | If separately adopted, preserve significant subjective surprise with expectation and evidence; no global judgment (PRED-05). | DEC-04 |
| 044 (proposed) | If separately adopted, conversational prediction callback is relevance/expiry/delivery-gated and at most once; distinct from adopted evidence-version reappraisal deduplication (PRED-05). | DEC-04 |
| 045 (proposed) | If separately adopted, low-value signal scheduling uses bounded passive range eligibility; no mandatory per-signal job (PRED-02/05). | DEC-04; existing range reuse |
| 057, 058 | Use owner-qualified task outcome/recurring evidence, not suggested work or absent reports, for completion expectations (PRED-01/07). | 06C integration |
| 082 | Evaluate predictive accuracy and approach suitability separately; correlate actual delivered approach and independent later feedback, allow unknown attribution (PRED-06/10). | P2-F2/P6-F2 |
| 083 | Persist forecast before outcome, resolve once against scoped evidence as hit/miss/inconclusive; inconclusive neither strengthens nor weakens expectation; separate later calibration (PRED-04/07). | P2-F1/P6-F2 |
| 085 | Include learned expectations, forecasts, suitability and derived projections in authorized end-to-end correction/deletion scope (PRED-01/06/08/10). | 07B/P6-F4 |
| 097 (proposed) | If separately adopted, formal confidence contract distinguishes model hint from owner method/version; predictive refinement independently requires no fabricated numerical certainty (PRED-02). | DEC-01; P2-F1 boundary |
| 106 | Revisions reflect new evidence and contextual drift without mutating stable identity or authority (PRED-07/08). | P2-F1/P6-F2 |
| 107 | Periodic longitudinal drift review uses independent evidence and distinguishes changed behavior from stale or missing observations (PRED-08/10). | P6-F2 |
| 113 | Selective expectation projection fits existing context budget; skip irrelevant or invalid context and retain fallback (PRED-09). | P2-F1/F3 |
| 117 | Preserve original evidence class, trust, verification and derivation references for expectations/outcomes/feedback (all PRED). | applicable 07B2/P2-F |
| 118 | Source correction, expiry or revocation invalidates dependent predictions, expectations and learned projections; stale worker cannot restore invalid inference (all PRED). | applicable 07B2/P2-F |

Shared acceptance: forecasts and learned expectations are interpretations, never verified facts or authority. Feedback must be attributable or unknown; negative reactions do not establish character traits. Include cross-instance denial, duplicate outcome replay, pre-outcome timestamp/version pinning, contradiction versus surprise, absent outcome, correction/revocation during inference, cold start, scope drift, explicit preference override and opt-out/reset. Use synthetic cases first, held-out and independent feedback for behavioral evaluation; record actual evidence in the matrix.

## 6. Roadmap integration and privacy

No changes to primary work-package assignments, order, initial Phase 0–1 MVP, existing exit criteria, or next scheduled increment. P2-F follows MVP-05 and applicable MVP-07B owner-specific lineage/correction contracts, not completion of every 07B or Phase 6 privacy item. As of reviewed main `827073d`, six 07B2 increments are merged; cognitive and pattern dependencies have bounded owner-specific coverage, but new predictive records and learned approach revisions still need their own eligibility/fencing. §6.1 defines admission; see the [roadmap P2-F work package](development-roadmap.md#p2-f--finish-cognitive-state-before-the-phase-2-exit) for the detailed increments.

| Existing milestone | Adopted scope |
|---|---|
| P2-F1.1 | Cognitive continuity and admission. Reuse cognitive lineage, pattern eligibility and owner commands; revisit unresolved questions, competing hypotheses and corrections across ranges. Implement the Authorization/Governance-owned learning control, authenticated commands, reset boundary and race fixtures (§6.1.1) before persistent learning. |
| P2-F1.2 | Contextual expectations. Minimal scoped versioned expectations, comparable evidence, maturity, counterexamples and drift. |
| P2-F1.3 | Prediction lifecycle and selective comparison. Pre-outcome forecast pinning, horizon, outcome correlation, idempotent resolution and relevant-signal comparison; optional next-act review, not speculative inference/prefetch. |
| P2-F1.4 | Subjective projection and conversational intentions. Existing Context Builder composes eligible beliefs/expectations, relationships, context and condition/expiry/relevance-aware intentions separate from user forecasts. |
| P2-F1.5 | MetaMemory and experience significance. Review 05D.5 and 07B2 gap/feedback eligibility; address only evidenced remaining knowledge-gap lifecycle, clarification and experience significance. |
| P2-F1.6 | Integrated cognitive acceptance. Cross-session cognitive/expectation lifecycle regression; review post-session experience, unresolved friction, corrections and goal alignment using existing owner findings. |
| P2-F2 | Separate immediate appraisal from mood; small reversible approach learning only after actual delivery correlation and attribution/unknown safeguards. |
| P2-F3 | Context budget and selection, correction/expiry/read fences, cross-session and restart behavior; measure relevance detection, deferred recovery and interactive overhead (§6.2). |
| 06C/D | Targeted integration with already delivered task outcome and strategy-support slices when needed; no reopening completed milestones or changing Phase 3 sequence. |
| P6-F2/F4 | Independent calibration, effectiveness/attribution, longitudinal drift, held-out regressions and full privacy propagation evidence. |

Before durable personalized learning, require the applicable scoped access, evidence lineage, correction, expiry, revocation and owner write/read fencing contracts. Honor user correction/removal, adaptation opt-out and reset; absence/withdrawal of evidence cannot become a negative trait. Do not retain revoked/deleted private content through derived views, logs or historical revisions. Do not publish private transcripts or learned profiles. Privacy-policy details (visibility UI, retention classes, deletion mechanics, consent/deployment) remain DEC-03 and MVP-07B; no release or compliance claim.

### 6.1 Predictive-learning admission contract (P2-F1.1)

Required for enabling durable real-user expectation/forecast or learned approach writes and their use in context. Reuse 07B2 third increment ([#135 cognitive source lineage](mvp-07b2-cognitive-lineage.md)) and sixth increment ([#138 derived projections](mvp-07b2-derived-projections.md)); use response #132 and raw foreground #136 delivery/input fences, summary #133 episode eligibility, and workflow #137 owner-qualified outcomes when those sources are consumed. These concrete merged prerequisites do not establish predictive-record implementation. Restrict initial source types to demonstrated owner-specific eligibility; exclude unsupported sources or legacy unknown dependencies rather than guessing. Review existing owner APIs; no generic provenance graph.

| Admission check | Required observable behavior |
|---|---|
| A-PRED-1 Scope and lineage | Host pins instance scope, evidence class/trust, exact source IDs/revisions or snapshot hashes and every consumed dependency, including counterevidence and delivered-response/outcome pins where relevant. Reject cross-instance, unsupported or unknown lineage; do not upgrade a report into verified completion. |
| A-PRED-2 Current validity | Source correction, supersession, expiry or revocation invalidates dependent expectation, forecast resolution, learned suitability and derived context; no fallback to invalid older version, no penalty from withdrawn evidence. Check selection, read and controlled output boundaries. |
| A-PRED-3 Late writes and replay | Revalidate dependency and learning-control version and reset generation (§6.1.1), selected generation/lease and idempotency at owner commit under existing short transaction/fence. Correction, revocation, opt-out or reset during inference rejects stale write; unrelated eligible scope survives; no duplicate resolution/revision from replay. No model/tool I/O under locks. |
| A-PRED-4 User control and retention | Explicit preference/correction overrides inference. The Authorization/Governance-owned control in §6.1.1 governs learning and use together: disable suppresses both; reset advances generation and permanently excludes older learned generations from personalization; re-enable resumes only eligible current-generation state. Authorized inspection and owner-specific correction/removal remain available under policy. Logical suppression is not full erasure. |
| A-PRED-5 Fail-safe conversation | Ineligible/missing expectation or disabled adaptation uses the predictive cold-start fallback while preserving explicit preferences, eligible Memory facts, instructions, stable persona and other authorized functionality. Current direct requests and mandatory correction/authorization gates win. No new per-turn inference or authoritative owner. |

P2-F1.1 implements and verifies the scoped control and authenticated command/inspection contracts in §6.1.1, and records allowed source classes, reusable test evidence and synthetic admission/race fixtures before P2-F1.2. Test the actual newly introduced owner write/read paths before each activation (P2-F1.2 expectations, P2-F1.3 predictions/resolution, P2-F1.4 projection and P2-F2 learned suitability); an unverified path stays disabled for real-user durable learning/use. P2-F2 additionally requires actual delivered-approach correlation and unknown attribution. Evidence: owner contract tests against actual PostgreSQL, exact tested revision, correction/revocation during preparation/commit/response delivery, restart/replay, cross-instance denial and opt-out/reset. Record PASS/FAIL/BLOCKED/NOT RUN and source exclusions in US-117/118 and checklist. Full US-085/07B3 deletion propagation is not a Phase 2 entry dependency; applicable policy and correction/withdrawal controls precede learning. End-to-end retention/deletion, external copies/backups and deployment gates remain unchanged.

#### 6.1.1 Learning control ownership and lifecycle

Owner decision (27 November 2026, PRED-ROADMAP-01 clarification): **Disabling learning disables both new derived learning and use of existing learned interpretations; reset is separate.** Disable is reversible suppression, not erasure. Re-enable resumes only current-generation state that still meets A-PRED-2. While disabled, predictive behavior uses the cold-start fallback, not a reset of the persona or its ordinary memory.

**Ownership and scope.** Authorization / Privacy Governance within existing Shared Infrastructure owns the authenticated control and its version/reset boundary. It does not own learned content. Cognitive owns expectations and forecasts, Relationship owns learned approach suitability, Memory owns evidence/retention, Conversation owns context/response and authorized inspection presentation, and Execution enforces control fencing. No new service, scheduler or cross-domain unrestricted write authority. Use the existing PostgreSQL `(user_id, instance_id)` key, where instance_id identifies the selected user–persona instance for execution; do not share controls across personas automatically.

Minimal logical record (physical schema and exact types of trusted boundary references settled in P2-F1.1):

| Field | Contract |
|---|---|
| user_id, instance_id | Unique scoped instance; authorized access only. |
| learning_enabled | Boolean; default false for new predictive-learning activation, missing/unknown control fails closed. Authenticated enable is required, subject to applicable policy. |
| version | Monotonic control revision; increment on effective enable/disable or reset. Capture for prepared work and controlled output. |
| reset_generation | Monotonic generation of learned state; advance only on reset. Learned records and their projections pin this value. |
| reset_boundary_ref | Host-assigned durable scope-wide evidence cutoff (nullable before first reset); identifies a causal boundary across eligible source streams, not a model date or an arbitrary conversation-local turn ID. No automatic reconstruction from evidence at/before the boundary, including historical summary/pattern inputs. If order cannot be established, exclude the source. |
| updated_at, source_event_id | Trusted timestamp and authenticated source/event provenance for the last accepted control change. |

Use existing owner commands, receipts, version checks and event/outbox transaction rules; the control is not a model proposal, ordinary inferred preference, application-session flag or global user profile. The implementation must define the trusted reset cutoff against actual event/owner source identifiers before activation; no new global journal is presumed.

| Operation (logical name) | Behavior |
|---|---|
| GetLearningControl | Authenticated scoped read of the setting, revision and reset state. |
| SetLearningEnabled(enabled, expected_version, idempotency_key) | Authenticated command; enforce scope and applicable consent/policy, change control version on an effective toggle. Repeated same-state request is a no-op; no new reset generation. |
| ResetLearnedState(expected_version, idempotency_key) | Authenticated command; atomically advance reset generation/version and establish trusted reset boundary; invalidate earlier generations for learning/use, leave enabled state unchanged. Replay does not reset twice. |
| InspectLearnedState | Authorized read-only inspection through respective owners, clearly identifying active, suppressed or historical/ineligible state where retention/access permit. Does not feed it into ordinary personalized context or enable learning. |
| CorrectLearnedState / WithdrawLearnedState | Authenticated owner-specific correction/withdrawal, with evidence and version checks; suppress/revise only affected interpretations. Does not enable learning or create a forecast retrospectively. |

**State transition rules**

| State/action | Required behavior |
|---|---|
| Disabled (including missing control) | No expectation/forecast formation, resolution for learning, maturity advancement, learned-suitability update, or personalized retrieval/comparison/projection from the governed state. Retain stored records only under applicable policy. Existing pending forecasts age/expire normally; silence/disabled intervals are not misses. Ordinary conversation, explicit preferences, Memory facts, instructions, approved goals/procedures and stable persona defaults continue under their own contracts. |
| Disable | Suppress current learned generation immediately at current eligibility boundaries, increment control version, invalidate prepared work and pending controlled delivery using the old version. No erasure or change to reset generation. |
| Re-enable | Increment version on actual toggle; allow only current-generation records passing current A-PRED-2 source validity, expiry, context and policy checks. Existing record creation version need not equal the latest control version; fresh use must capture the latest control fence. No automatic revival of reset generations or expired/withdrawn records. A long pause is possible drift, not evidence of failure; reassess sufficiency before reliance. No automatic maturity advancement or replay of disabled-period evidence to catch up learning. |
| Reset while enabled or disabled | Advance generation and establish boundary atomically; earlier generations remain permanently ineligible for personalized learning/use, including reconstruction through derived evidence. Do not delete ordinary Memory, explicit preferences, instructions, goals, procedures or persona identity. Authorized inspection/retention is distinct from personalization eligibility. Re-enable alone cannot undo a reset. |

The governed scope is derived predictive expectations, forecasts/resolutions, learned conversational approach suitability and their projections or downstream learned effects. Do not suppress independently eligible factual evidence merely because it was once used for learning. Explicit user preferences continue to override inference. General reflection, evidence storage and existing authorized tasks/procedures remain available; they cannot bypass this control to recreate or use the governed learned state.

**Fencing and acceptance.** Capture control version, reset generation and eligible source dependencies at preparation. Recheck all under the current owner transaction and existing selected-generation/lease fences at commit; serialize the final check with control changes, using a documented lock order and no model/tool I/O under locks. Apply the same control and generation eligibility at retrieval, comparison, context assembly, response commit and controlled offer/ACK/delivery (including replay and speech where applicable). Previously delivered content cannot be retroactively undelivered. Disable/enable/reset cannot revive stale prepared operations even if the boolean has returned to its former value. Use the existing historical/response lineage mechanism or its minimal owner-specific extension rather than a new parallel scheduler. Publish versioned control changes through the transactional outbox; delayed consumers must recheck current authority, not rely on notification timing.

P2-F1.1 implements the minimal control/commands and synthetic race tests; each learned path proves actual read/write/delivery integration before activation. Required PostgreSQL checks (A-PRED-3/4/5): absent record defaults off; cross-instance and unauthenticated access denied; explicit enable; no-op and idempotent retry; disable during inference and between response commit/offer/ACK; disabled state survives restart and has no retrieval/comparison/maturity change; re-enable same generation with current valid evidence only; correction, expiry and revocation during pause; reset both enabled and disabled; old-generation worker rejection after re-enable; no reconstruction from pre-reset evidence (including mixed derived ranges); unrelated instance/explicit preference/ordinary Memory behavior unchanged; read-only inspection does not activate learning. Test that disabled replies use predictive cold-start behavior without losing known explicit user information. Do not declare A-PRED-4 passed based on existing relationship preference or session revocation tests.

Implementation details still open: physical table/schema, exact cutoff encoding and lock ordering, inspection UI, actual retention/erasure policy and measured drift thresholds. The initial default-off and no automatic disabled-period catch-up are conservative implementation decisions for this control; review against deployment policy during P2-F1.1. This is not a global memory opt-out or authorization to change other learning features.

Implementation candidate: [P2-F1.1 Governance control and admission](p2-f11-predictive-learning-admission.md). The initial raw-text generation stamp and response fence are implemented for synthetic verification; actual learned-content paths and policy/consent activation remain gated. See the candidate's source exclusions, lock ordering and NOT RUN results.

P2-F1.2 [contextual expectation candidate](p2-f12-contextual-expectations.md) adds the narrow raw-report Cognitive owner path; provisional pilot policies and remaining Memory-pattern/activation requirements are explicitly distinguished from approved PRED criteria. Tests NOT RUN.

P2-F1.3 [prediction lifecycle candidate](p2-f13-prediction-lifecycle.md) implements the bounded dated-activity forecast, ingress-time causal bindings, owner resolution/withdrawal and selective diagnostic comparison. Pilot horizon, logical expiry and strict pause policy are provisional; tests NOT RUN and no actual personalized projection.

### 6.2 Relevance gate and measured acceptance (P2-F3)

Conversation's existing sparse interpretation uses active topic/referents, pending commitments/forecasts, outcome/achievement reports (including indirect reports), explicit corrections and meaningful behavioral-change cues. Cognitive validates retrieved expectation applicability: scope, horizon, maturity, source eligibility and whether incoming evidence matches its target. A cheap deterministic or existing lightweight/model inference signal suffices when supported; no mandatory classifier or model pass. Mandatory corrections, authorization, direct requests and required state transitions cannot be skipped by this optional relevance decision.

| Local decision | Meaning |
|---|---|
| COMPARE_NOW | Meaningful and timely candidate; bounded eligible lookup and comparison within interactive budget. If no eligible expectation applies, return UNKNOWN; if not compared, record actual abstention/deferral reason. |
| DEFER | Potentially useful but ambiguous, low-value or over budget; retain eligible event for existing batched cognition, without claiming immediate comparison. No per-signal job required. |
| SKIP | No extra immediate work; omit the hint. Durable event remains eligible for later watermarked reflection under privacy policy. |

These are local labels, not persisted Timing values, lifecycle states or scheduling authority. DEFER may use existing PASSIVE semantics or an owner-admitted operation when warranted; SKIP means no emitted hint, not durable SKIP mode (CRA-08 boundary, without adopting unrelated CRA proposals). Keep routing separate from EXPECTED/UNEXPECTED/CONTRADICTORY/UNKNOWN and actual Emotion reaction. A later discovery can revise expectations without retroactively claiming a timely appraisal or sending an unsolicited callback.

P2-F1.3 defines routing and deterministic regression fixtures; P2-F3 measures the integrated pipeline with held-out independently labeled synthetic scenarios (and explicitly permitted real-model evidence where applicable). Include direct/indirect achievements, ambiguous references, corrections, topic switches, duplicate outcomes, cold start, stale/ineligible expectations and unrelated greetings. Label whether comparison is warranted, whether an eligible prior expectation applies, whether timely appraisal is useful and whether deferral is appropriate. Label against evidence available before response, not hindsight. Compare with baseline at equivalent model/hardware, context and workload; preserve budget and privacy.

| Metric | Measurement |
|---|---|
| Immediate relevance recall | Relevant timely candidates routed COMPARE_NOW / all labeled timely candidates. Report false negatives by DEFER/SKIP. |
| Immediate relevance precision | Labeled timely candidates routed COMPARE_NOW / all COMPARE_NOW routes. Report false positives and eligible-retrieval abstentions. |
| Missed immediate appraisals | Count and rate among labeled opportunities with eligible prior expectation and appropriate immediate reaction; distinguish routing misses, retrieval/eligibility failures, comparison errors and presentation omissions. Not every supported mismatch requires visible expression. |
| Deferred recovery | Eligible missed/deferred learning opportunities later identified by watermarked cognition / eligible missed/deferred opportunities whose declared observation window elapsed. Report horizon, pending/inconclusive cases and time-to-recovery; pending is not failure, recovered is not timely. |
| Unnecessary retrieval | Irrelevant turns causing expectation retrieval / labeled irrelevant turns; also report retrieval/model-call counts, context cost and abstentions. |
| Interactive overhead | Paired baseline versus enabled p50/p95 latency to first content and completed response (or clearly labeled available proxy), with model/hardware, routes and sample counts. Include query latency, contention and budget-stop reasons. |

Report numerator/denominator, labels/adjudication, tested SHA, model/hardware and measurement limitations; use N/A for empty denominators. Establish baseline and agree thresholds before declaring P2-F3 accepted. Mandatory correctness/scope/authority and no stale-context regression remain hard invariants. No speculative next-intent prefetch requirement, universal threshold, new evaluation service or optimization for engagement. Independent quality review remains distinct from deterministic tests.

## 7. Decisions still open

Evidence thresholds and context taxonomy, decay and horizon policies, visibility/correction UX, retention and deletion implementation, calibration when independent ground truth is unavailable, optional prefetch cost/benefit, exact storage schema and affect representation. Do not set these before owner contract review and measurement. Proposed stories 043/044/045/097 and other DEC-01–07 details remain separately governed. No runtime tests or implementation acceptance executed for this documentation decision.
