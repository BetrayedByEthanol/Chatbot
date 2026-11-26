# MVP-06D — Authorized strategy research and diagnostic adaptation

Architecture v2.5.1 §§6.2–6.3, 21.3–21.4, 22.1–22.3; confirmed US-049/050/052/053/056. Baseline `965c8ceaf9e583fa2733fbd8a30e25d402f12d94`, merged PR #129 technical CI 2,253 passed, 3 skipped, live integration passed. This increment closes a local read-only research-to-candidate gap and adds evidence comparison and diagnostic guidance, not a general autonomous researcher or method optimizer.

## Research and adoption

The repository has registered clock and text-statistics tools, but no external research provider. The bounded route uses the existing Knowledge owner and eligible, current, private supplied-document passages only; no URL fetching, network/model calls, permission changes or new scheduler. Research sources are supplied text, not independently verified findings. The explicit local lookup is an immediate typed read, not a Task/Operation grant or durable research job.

1. `ResearchGoalStrategiesCommand` binds the approved goal ID/version, a bounded search query, authenticated evidence and separate exact Goal approval. `research_goal_strategies` checks selected Foreground generation, both source/approval generations, authoritative lineage, active ancestry and goal version. It reads up to four current eligible passages under the existing Knowledge policy (normal privacy, active, unexpired, exact source integrity). No source means NO_ELIGIBLE_SOURCE; no invented answer, no candidate, no goal mutation. Each live read rechecks eligibility; it does not promise a replay of a historical snapshot.
2. The caller reviews actual excerpts and explicitly proposes an approach, suitability note and limitations with `ProposeResearchStrategyCommand`. Its separate exact approval includes the approved research request and one to four document/version/position/content-digest pins. These are provenance, not proof that the approach works, safe semantic advice or an independently validated citation-to-claim inference. No model automatically copies or synthesizes document text into a candidate. The owner checks research/strategy scope, current goal version and active ancestry, both approvals, and exact currently eligible pins using the Knowledge owner. A changed, withdrawn, restricted or missing source is rejected. Source selection is exact and not dependent on unrelated search reranking.
3. Existing `propose_goal_strategy` persists the CANDIDATE and scoped immutable links in the same receipt/event/outbox transaction. Proposal does not select or execute. Existing `select_goal_strategy` requires separate exact selection approval, active ancestry, current goal version and current source eligibility for research-backed candidates. Document locks fence concurrent revision/withdrawal until the short owner transaction commits. Previously authorized selection history stays intact if evidence later becomes unavailable; no automatic deselection, procedure mutation or goal drift. The user can consider another independently approved strategy.
4. `compare_goal_strategies` returns the selected candidate first and up to 12 alternatives with title, approach, suitability note, research limitations, current cited source passages (or SOURCE_UNAVAILABLE/UNSOURCED), and up to 12 latest per-run observations per strategy. Each observation pins template/version and private variant/revision; latest corrected assessment replaces prior assessment in this read, and a mismatched run version is STALE rather than current success. No aggregation of unlike methods or false population-level success rate, no automatic rank or verified effectiveness. Truncation is explicit. Goal outcome and criterion are unchanged.

Migration 0055 adds `goal_strategy_research` and `goal_strategy_research_sources`; scoped foreign keys bind candidate, approved turns, source version and passage, and existing event. Metadata is append-only; source text remains in Knowledge rather than duplicated. User-approved approach and limitations are distinct strategy fields; source withdrawal does not erase independently supplied user text or historical decisions. No automatic extraction or external disclosure. Existing ordinary user-requested strategy proposal/selection remains compatible.

## Diagnose first, then consider the smallest change

Existing P3-05 evaluation, private adaptation and exact decision contracts already implement versioned evidence, STABLE diagnosis, validated parameters and bounded ADAPTIVE overrides. `read_adaptation_guidance` reads the pinned definition/current private revision and latest assessment in a scoped snapshot. It proposes a next review only, never a concrete value, permission or method mutation:

| Evidence | Next review |
|---|---|
| No current assessment / stale run or variant | Assess / reassess before relying on evidence |
| Unknown or undetermined failure | Clarify cause |
| Incorrect input | Check inputs |
| Environment | Check environment |
| Incomplete action | Complete required action |
| Invalid criteria | Review criteria; do not silently change goal |
| Reported success | No change |
| Method mismatch, STABLE | Diagnose fixed method, no rewrite |
| Method mismatch, private variant with parameters | Consider parameter change first |
| Method mismatch, ADAPTIVE private variant without parameters | Consider private variant adjustment |
| No private adjustment path | Consider alternative method |

The existing explicit Procedure adaptation proposal/decision remains the only write path for this guidance: latest negative or incomplete outcome, matching pinned run/version and private revision, validated template policy, current selected strategy and exact separate approval on APPLY. A proposed or applied revision is not successful execution. New runs may pin a new private revision; old runs and shared definitions remain unchanged. The guidance is conservative and advisory, not a new prohibition on existing explicitly approved owner commands, an automated diagnosis or an exhaustive parameter-search algorithm. It does not derive a user commitment from a failure classification. No goal/Task/Operation authority changes.

## Regression and boundaries

`tests/test_mvp06d_strategy_research.py` exercises exact approval and query tampering, foreign/restricted sources, no eligible source, forged digest, separate candidate/selection, withdrawal and changed version, scoped immutable links and replay, alternatives with latest per-run observations, stale assessment and private revision pins, cause-specific guidance and existing approved parameter adaptation, unchanged goal/shared method and no operation. Existing P3-01, P3-03–05, Knowledge and integrated acceptance remain mandatory.

```sh
python -m pytest -q tests/test_mvp06d_strategy_research.py \
  tests/test_phase3_goals.py tests/test_phase3_learning.py \
  tests/test_phase3_procedures.py tests/test_mvp05d4_document_knowledge.py \
  tests/test_phase3_integrated_acceptance.py
```

Candidate static review only; PostgreSQL and live CI NOT RUN at authoring. No real-model acceptance or external research provider. The local supplied-source route does not satisfy generalized external Task/Operation research, semantic support/safety review, natural conversational approval, autonomous strategy selection, statistical effectiveness evaluation or cross-context learning. US-050 remains M at authoring pending technical verification; other statuses unchanged. After passing technical CI, focused MVP-07A may proceed; full Phase 2/3 acceptance and deployment gates remain open.

## Subsequent verification (26 November 2026)

PR #130 merged as `1cf551755e0478bff19bf2efde1199a490ecf634`; final head `2507af4aa0e2c3195c7ec2dc9c3b9e29d691f201` passed [PostgreSQL CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45825422385) (2,261 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45825422330). Candidate authoring-time NOT RUN is superseded for technical CI only. Local research does not implement external provider admission or quality acceptance. Focused 07A proceeds.
