# Predictive user modeling and experience-driven adaptation — adopted roadmap refinement

**Decision:** PRED-ROADMAP-01, 26 November 2026. Project owner approved adoption of the 26 November predictive-user-modeling draft with targeted revisions, without changing the existing development sequence.  
**Status:** Approved planning scope and story-criteria extensions; not an architecture amendment, a runtime implementation, or acceptance evidence.  
**Baseline reviewed:** `main` at `f229600d24293d0cdc2883690e5d122b1cc827a5`. Architecture v2.5.1 and the reconciled 124-story backlog remain authoritative. The existing [roadmap](development-roadmap.md), [matrix](user-story-implementation-matrix.md) and [MVP checklist](mvp-acceptance-checklist.md) retain their milestones, phase exits, story IDs, authority and priorities.

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
| PRED-04 | Selectively compare relevant new evidence to an applicable preexisting expectation: EXPECTED, UNEXPECTED, CONTRADICTORY or UNKNOWN. Ordinary greetings and unrelated turns need no lookup or comparison; no applicable evidence is UNKNOWN. A surprising outcome need not contradict a fact or instruction. | Cognitive / Conversation; P2-F1 |
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
2. On relevant signal only, the existing Context Builder retrieves a budgeted eligible projection. `PredictiveUserContext` is a logical read-only contribution to the existing contextual/subjective projection, complementary to persona-self/affect context; no independent pipeline, store or service. It contains context, maturity, applicable horizon and eligible provenance/revision refs, not free-form character judgments.
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

No changes to primary work-package assignments, order, initial Phase 0–1 MVP, existing exit criteria, or next scheduled increment. P2-F follows MVP-05 and applicable MVP-07B owner-specific lineage/correction contracts, not completion of every 07B or Phase 6 privacy item. The 07B2 response and summary-lineage work is reusable but does not yet establish transitive reflection/prediction lineage.

| Existing milestone | Adopted scope |
|---|---|
| P2-F1 | Minimal scoped expectation/forecast lifecycle, meaningful-signal comparison, evidence sufficiency/revision/drift and optional projection; review GAP-02, 05/06 and remaining MetaMemory gaps. |
| P2-F2 | Separate immediate appraisal from mood; small reversible approach learning only after actual delivery correlation and attribution/unknown safeguards. |
| P2-F3 | Context budget and selection, correction/expiry/read fences, cross-session and restart behavior; measure interactive overhead. |
| 06C/D | Targeted integration with already delivered task outcome and strategy-support slices when needed; no reopening completed milestones or changing Phase 3 sequence. |
| P6-F2/F4 | Independent calibration, effectiveness/attribution, longitudinal drift, held-out regressions and full privacy propagation evidence. |

Before durable personalized learning, require the applicable scoped access, evidence lineage, correction, expiry, revocation and owner write/read fencing contracts. Honor user correction/removal, adaptation opt-out and reset; absence/withdrawal of evidence cannot become a negative trait. Do not retain revoked/deleted private content through derived views, logs or historical revisions. Do not publish private transcripts or learned profiles. Privacy-policy details (visibility UI, retention classes, deletion mechanics, consent/deployment) remain DEC-03 and MVP-07B; no release or compliance claim.

## 7. Decisions still open

Evidence thresholds and context taxonomy, decay and horizon policies, visibility/correction UX, retention and deletion implementation, calibration when independent ground truth is unavailable, optional prefetch cost/benefit, exact storage schema and affect representation. Do not set these before owner contract review and measurement. Proposed stories 043/044/045/097 and other DEC-01–07 details remain separately governed. No runtime tests or implementation acceptance executed for this documentation decision.
