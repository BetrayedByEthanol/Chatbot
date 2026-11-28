# MVP-07B2.10 — Consolidated acceptance and performance

Baseline: main `010e99f572dce6cbf20358245023c972eadf7b9b` (merged #143).
Scope: trusted local synthetic PostgreSQL testing; not release, deployment or
physical-erasure acceptance. Architecture v2.5.1 and the reconciled backlog
(PPA-US-117, 118, 085) retain their authority. This is a closure candidate,
not a blanket acceptance of all derived copies.

## 1. Executed historical evidence

| Increment | Tested PR head | PostgreSQL result | Live integration |
| --- | --- | --- | --- |
| 07B2.6 #138 | 5854636 | [2327 passed, 3 skipped, 720.23 s](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45894540960) | [PASS](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45894541044) |
| 07B2.7 #141 | 116fcb8 | [2351 passed, 3 skipped, 563.95 s](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46069475672) | [PASS](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46069475666) |
| 07B2.8 #142 | 4dedb37 | [2364 passed, 3 skipped, 773.13 s](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46081462359) | [PASS](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46081462223) |
| 07B2.9 #143 | 8809513 | [2384 passed, 3 skipped, 886.22 s](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46084996750) | [PASS](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46084996772) |

Job logs and conclusions inspected, not independently rerun. #143 Phase 0
also passed five standalone contracts and 1003 tests without Psycopg (262
skipped). Test time excludes setup. PR merge-ref runs are not separate
execution on the resulting main merge SHA. The old #141 performance fix
intermediate head a48a884 ran 2342 tests in 853.54 s in CI (user-supplied log);
the final head ran faster. Runner variability and incremental test work make
cross-run timings indicative, not a controlled benchmark. From #138 to #143
the wall time increased about 23%, with about 2.4% more passing tests. The
proposed 20% full-suite tolerance is not demonstrated on matched hardware.
No cause assigned to an individual domain from aggregate duration alone.

## 2. F01–F12 acceptance matrix

PASS below means the cited bounded regression suite ran successfully, not
that every conceptual requirement is exhaustive. Candidate changes and new
regressions are NOT RUN until the new head's actual execution evidence exists.

| Finding | Executed evidence from merged suite | Residual risk / new gate |
| --- | --- | --- |
| F01 Dialogue metadata | test_mvp07b2_dialogue_entity_lineage: source/hint withdrawal, independent referent, scoped lookup | Candidate fixes independent pending act/referent loss when title is invalid. Resolution status and late explicit/ingress mutation reappraisal remain open. |
| F02 Intent | same module: linked root and model-derived dependencies; test_resolved_objective_dependency_budget | Atomic source/text budget for selected corrections. Corrections outside existing three-detail window and generalized semantic reference remain open. |
| F03 Affect | test_mvp07b2_affect_relationship_reappraisal | Applied cue replay and cross-session fences; bounded stale foreground replay, idle repair and long-lived same-persona cost remain to measure. |
| F04 Relationships | same module; test_phase2_affect | Effective preference/rapport, no superseded fallback, presentation-only recent continuity; conservative broad fences and full scoped rapport replay remain. |
| F05 Entity | test_mvp07b2_dialogue_entity_lineage; test_mvp05c1_entities | Exact revision, alias and relationship invalidation; unsupported semantic entity resolution and old unbound projections remain. |
| F06 Tool outcomes | test_phase1_tools: worker/ready/delivered/direct withdrawal | Controlled consumption protected; no recall of already observed/external output, historical attempt results may remain. |
| F07 Goal evidence | test_phase3_goals; test_mvp03a4_goal_autonomy | Effective counts/context and bounded worker pins. Previously accepted derived goals require owner reassessment, not automatic revocation. |
| F08 Memory extraction | test_phase2_extraction; test_mvp02c2_reviewed_model_memory | Delayed and reviewed source checks; committed-source vs dispute-policy parity needs continued verification. |
| F09 Research | test_mvp06d_strategy_research | Candidate fixes ordinary strategy list leaking unavailable research text. Complete selection-approval chains and already authorized effects remain bounded. |
| F10 Background | test_mvp03a4_goal_autonomy; existing P0/ALIGN-01/AGENT contracts | Late Goal results and final readiness checks. Already READY epochs and historical critical-owner decisions are not universally reappraised. |
| F11 Notifications | test_phase4_notifications | Linked due/inbox/snooze withdrawal; no provider erasure or recall of delivered copies. |
| F12 Performance | historical CI and #141 scoped candidate guard | New scoped and affect benchmark/report NOT RUN. Longer owner histories and concurrency/target-host budgets remain open. |

Other 07B2.1–6 evidence remains in checklist section 2K; do not discard
response, summary, cognition, raw history, workflow and derived projection
contracts. A bounded pass cannot waive their acceptance criteria.

## 3. Concrete acceptance corrections in this candidate

- Foreground active dialogue no longer makes independently eligible child
  metadata depend on the title's admission. If the title is absent or
  unfenced, show an omission marker only with admitted independent metadata;
  never expose unfenced thread status or next-action text. The original
  active title remains absent from Goal focus routing. No historical mutation.
- Ordinary Goal strategy listing now evaluates the research eligibility of
  selected bounded candidates and masks unavailable research using the same
  placeholder as comparison, retaining strategy identity/status. No
  automatic strategy selection or goal cancellation.

No migration, new service, model, cache, or vector backend. Existing
PostgreSQL authority and owner transactions remain unchanged.

## 4. F12 repeatable measurement

New `test_mvp07b2_acceptance_performance.py` measures the actual focused
thread query and complete `prepare_text_context` call with a pending turn,
three warmups and twenty measured calls per stage. It records p50/p95/max
client round-trip time (not model latency), JSON EXPLAIN ANALYZE BUFFERS
plans/settings, buffer hits/reads, PostgreSQL version, planner settings and
signal counts. The same target is probed before and after adding unrelated
user conversations and same-persona other conversations, then same-conversation growth with an
identical new pending request and explicit refocus of the original thread.
The existing shared suite database may already contain other records. The deterministic
acceptance assertion counts actual eligibility calls using a rolled-back
transaction-local instrumented predicate: no unrelated turn can be checked
and the count must not increase. Timings are intentionally diagnostic, not a
machine-dependent hard assertion.

Default: 32 new conversations per separate-conversation growth stage and
32 additional objectives in the target conversation through production
ingress/delivery. For a larger standalone run set `PERSONA_07B2_SCALE=256`
(max 1024 per stage). The additional fixture work contributes to total suite
duration but is not included in query timing. A second probe creates
129 real applied Emotion cues and Relationship observations, measures
current presentation, stale over-limit presentation and explicit owner
snapshots; a guard ensures the two foreground paths never enter the
full appraisal replay. Both probes record three warmups/twenty samples.
These are bounded integration workloads, not production-scale benchmarks.
No full model call, network or concurrent worker load is timed. Longer
owner histories and controlled concurrent worker pressure remain to measure.

Run on a **fresh disposable database** with installed test extras and
`TEST_DATABASE_URL` set (never point at application or production data):

```bash
PERSONA_07B2_SCALE=256 python -m pytest \
  tests/test_mvp07b2_acceptance_performance.py -vv -s \
  --junitxml=artifacts/07b2-performance.xml -o junit_family=xunit1
```

Reset the disposable database before another pytest invocation; the session
fixture applies all migrations. Capture identical machine, PostgreSQL
version/configuration, dependency versions and scale for comparisons.
Do not reset a production database. Full suite:

```bash
python -m pytest -q -rs --durations=30 \
  --junitxml=artifacts/postgres-contracts.xml -o junit_family=xunit1
```

Phase 0 CI now retains the JUnit artifact (including benchmark properties)
and prints slowest tests and skip reasons. Compare full-suite timings on
matched hardware; inspect any change in candidate work and p95. Proposed
focused lookup p95 <10 ms and full-suite +20% tolerance are working targets
from prior diagnosis, not agreed universal release thresholds. Do not waive
a correctness failure for speed, disable eligibility, or tune PostgreSQL
globally to hide a bad join.

## 5. Acceptance decision and next gates

Current decision: **TECHNICAL BASELINE PASS / CLOSURE NOT ACCEPTED**.
New corrections, performance probe and CI artifact path require execution.
Run full contracts and live acceptance on candidate; inspect all skips,
benchmark plans and slow tests. Review late owner/Memory lock ordering,
expiry-only boundaries, same-persona growth and remaining state-specific
gaps before full B2.5 closure. Existing bounded deterministic regressions do
not prove general model behavior or exhaustive concurrency safety.
Record independent review and explicit operator decision rather than
assuming merge equals full acceptance.

US-117 and US-118 stay P (partial), US-085 stays M (missing end-to-end
implementation). Original backlog criteria include source trust/revision,
no authority elevation, retrieval exclusion and late-write fencing, and
policy-governed retention/deletion/backups/providers; latter remain 07B3 and
DEC-03. P2-F1.1 predictive learning admission and Q08 target-host model
performance are separate gates, NOT RUN by these regressions. Remote/untrusted
deployment remains blocked. No user policy, retention period, permission,
release or physical erasure inferred.

Candidate head / CI result / benchmark values / independent reviewer:
NOT RUN, to be recorded from actual subsequent evidence.


### First closure-candidate CI run (41ea5c0)

[Phase 0 run 46087006482](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46087006482):
2386 passed, 1 failed, 3 skipped in 772.71 s. [Live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46087006468) passed. The focused scaling and both read-path regressions passed. The affect benchmark failed during fixture setup (32 of 129 applied/recorded inputs), before latency measurement. The test incorrectly equated a 64-delivery claim bound with the number of inputs dispatched by a tick: dispatch advances one event per stream per invocation, and the fixture produces supersession events between inputs. Four ticks at 16 dispatch passes explain the observed underfilled queue.

The corrected fixture drains the real outbox in bounded passes before
claiming, verifies 129 pending input deliveries for each isolated consumer,
then requires exactly 129 persisted applied appraisals and observations.
No worker bounds, production behavior, or replay-limit assertions are
changed. The 3 skipped tests require optional NumPy (two ONNX and one
streaming ASR test). Slowest test was the existing phase4 preparation
Memory conflict test (72.07 s); investigate separately if reproducible.
The [JUnit artifact](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/46087006482/artifacts/15240465041)
was uploaded even on failure. New execution and actual benchmark values
remain pending; no full acceptance.
