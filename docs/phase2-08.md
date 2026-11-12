# P2-08 — Phase 2 Integration & Acceptance

Architecture authority: **Persistent Persona Agent Architecture v2.5**, Section 14 (Phase 2 exit gate), §§17–20, 23 and 24. This milestone verifies *integrated, domain-owned persistent cognitive state*. It does not extend Phase 2 into Phase 3 goal/procedure learning, Phase 4 background autonomy or Phase 5 multimodal delivery.

## Explicit Phase 2 exit gate

Section 14 states:

> Long conversation survives compression and restart; two independent cognitive updates do not overwrite each other.

The required demonstration combines all three supporting properties: persistent evidence provenance across sessions, independent async/domain state changes, and safe selected-persona recovery. Passing each isolated P2 test is necessary but is **not sufficient** evidence of this cross-domain contract.

## Acceptance evidence matrix

| Contract | Integration evidence | Boundaries / additional preexisting coverage |
|---|---|---|
| 50+ turns across compression and process/connection restart | `test_phase2_50_turn_compression_topic_restore_and_provenance`: 50 original user turns, six bounded source-linked summary operations, 48-turn watermark, retained original evidence and restart read | Existing `test_phase2_summaries.py` covers nonoverlapping ranges, long summaries and generated-but-undelivered responses |
| Cross-session evidence recall without inventing facts | `test_phase2_same_conversation_independent_domain_updates_and_restart`: memory plus model-interpreted episode, each with immutable source-turn IDs and distinct epistemic labels | `test_phase2_retrieval.py` covers temporal, privacy and scoped retrieval; P2-04 gates sensitive auto-admission |
| Simultaneous independent cognitive updates | The same test runs **P2-02 summary** and **P2-06 cognition** concurrently via distinct P0 operations sharing a bounded `cognition.llm` resource pool; verifies both watermarks, source events, reflection and episode persist | P0 and P2-06 suites cover lease fencing, retries, operation reaping, authorized terminal recovery and deadlock lock order |
| Cross-domain isolation & user authority | The same conversation yields **P2-04 safe memory**, **P2-07 Emotion** and **P2-07 Relationship** state, and separately stores a tentative **P2-06 hypothesis** only in INTERNAL proposal records; no cross-domain promotion | Versioned preference updates, explicit SET/REMOVE precedence, sensitive-topic exclusions and exact provenance are covered by P2-04/P2-07 |
| Persona switching, deferral and recovery | `test_phase2_switch_defers_other_domain_events_without_cross_persona_leak`: switch away before event processing, defer both owners, reselect, replay exactly once, other instance remains untouched | Existing P1/P2 tests verify stale model generation, in-flight cognition cancellation and selected-persona fencing |
| Foreground latency/isolation | `test_phase2_foreground_selectivity_does_not_run_background_models` fails if ordinary context assembly invokes summary/cognition model work or unrelated historical topic retrieval; checks bounded context and last-user-turn preservation | Separate live Ollama integrated workflow measures `output_ready_p95_ms` and `delivery_p95_ms` on an actual model; no fake-model latency is reported as production performance |
| Restarts, idempotency and P0 recovery | Cross-domain scenarios reconstruct state on **fresh PostgreSQL connections**; original P2 domain tests still exercise crash after staging, crash after commit, delivery replay, stale operation leases and outbox | No new recovery daemon or second scheduler is introduced |

## Test entry points

CI already executes real PostgreSQL integration tests and a separate real Ollama acceptance workflow:

```sh
# With PostgreSQL and ordered migrations through 0013 installed:
pytest -q tests/test_phase2_acceptance.py

# Existing complete CI gates (do not replace with the four focused tests):
# Phase 0 contracts -> full PostgreSQL tests and standalone contracts
# Phase 1 live integrated acceptance -> real Ollama model and transport
```

The new P2-08 suite uses deterministic model gateways **only for evidence-linked summary and cognitive proposals**. It does not claim that the deterministic responses validate model interpretation quality, affect calibration or subjective experience accuracy. The live model workflow validates actual model connectivity and delivery behavior independently.

P2-08 makes no new migration. All Phase 2 data remain in their owning PostgreSQL tables, with their existing event/outbox, version and scope contracts.

## Latency measurement protocol

Foreground responsiveness must be measured separately from deep reasoning and worker backlogs.

- **Hot path guard:** no unneeded optional inference or history lookup in an unrelated foreground context build; the test emits an informational local-context duration, **not** a cross-hardware SLO.
- **Live acceptance:** preserve actual-model `output_ready_p95_ms` and `delivery_p95_ms` output from the live workflow. Record model/hardware and sample size before setting an operational latency target.
- **Background contention:** concurrent summary/cognitive jobs must not block the caller from ingesting a new turn; P0 owns resource reservation and cancellation; avoid any DB transaction spanning an LLM call.
- **Regression policy:** if a latency threshold is proposed later, benchmark on defined hardware with multiple samples, controlled load and representative context lengths. Do not infer a product SLO from one CI run.

## Phase 2 boundary and decision register

| Decision | Status |
|---|---|
| Reuse PostgreSQL domain-owned Memory, Conversation, Cognition, Emotion and Relationship state | Confirmed (no schema redesign) |
| Keep stable persona identity separate from mutable mood and tentative user-affect estimation | Confirmed |
| Keep hypotheses, interpretations and subjective episodes below verified memory evidence | Confirmed |
| Separate conversation, cognitive and goal/task attention; do not consolidate into a global priority scalar | Confirmed |
| Continue hybrid deterministic/LLM, bounded foreground inference and asynchronous cognition | Confirmed |
| Include full goal/subgoal/strategy/procedure evaluation and durable procedure progress in Phase 2 | **No** — Phase 3 |
| Implement strict Background entry barrier, autonomous research, reminders and task continuation in Phase 2 | **No** — Phase 4 |
| Implement streaming TTS, interruption delivery and output-channel policies in Phase 2 | **No** — Phase 5 |
| Calibrate emotions, develop long-horizon subjective interpretation revision, adopt cognitive proposals into owners automatically | **Not accepted as complete** — evaluate and schedule separately; no implicit promotion |
| Declare P2-08 complete without real PostgreSQL and real-model CI plus PR review | **No** |

The v2.5 Section 24 scenarios include later-phase cases (50-turn guided procedure, goal adaptation, TTS barge-in, autonomous research). Their absence from the Phase 2-specific exit gate is **not** evidence that they are complete.

**Release criterion:** only after the P2-08 cross-domain suite and all existing CI checks pass, the acceptance matrix is reviewed, and the PR is merged can Phase 2 be recorded as complete. Do not merge automatically.
