# MVP-05E — Integrated acceptance and consolidation

Architecture v2.5.1 §§5, 14, 18–19, 22; roadmap MVP-05. This continuation increment implements the preceding acceptance review, not an architecture amendment, additional mandatory initial MVP gate or release decision.

## Baseline and classifications

Reviewed `main` at `58fcc015c5ffd3e9b465ac248be2dcdf9b4e0439` after PR #124. Verified tested PR head `e67636a497fec179fab7f9e6aadd0fe916319076`: [PostgreSQL CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45784981485) 2,136 passed, 3 skipped; [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45784981414) successful with real qwen2.5:0.5b (four-turn technical integration; output-ready p95 1492.14 ms, delivery p95 1516.13 ms, informational only). Not separate execution on merge SHA, not a target-host SLO. No independent quality acceptance.

The 12 primary stories are reassessed against the reconciled backlog: 029/030/027/028 retain I; 093 (M→I), 032 and 033 (P→I) reflect implemented core in scoped entities, versioned meaning spans/inferred links and distinct episode/pattern/document stores. 091/113/031/034/092 remain P. Total 7 I / 5 P / 0 M, project totals 48/57/19 from the original 45/59/20. Other 112 stories not reassessed. I is a static core-implementation classification, not acceptance of every criterion.

## Consolidation changes

Migration 0051 adds source-linked, value-free notices for complete explicit definitions of already-supported topic names whose proposed value fails the existing admission grammar. The original authenticated source turn/event is retained; no unsupported replacement value is copied, candidate admitted, or owner record modified. The shared read eligibility SQL suppresses an older same-slot meaning and its exact report/episode sources; the notice itself is not generic recalled evidence. The historical read revision increments at commit. Only a subsequent eligible supported owner revision with newer evidence clears the routine-planning barrier. Explicit routine-use questions while unresolved omit optional history and return canonical clarification at response commit, even if the model guesses a plan. Direct definition recall abstains; MetaMemory records a disputed gap. A later successful recall observes resolution. No new model pass, worker, arbitrary definition admission or goal/procedure authority.

This is conservative whole-statement syntax, not general correction inference, raw-transcript erasure or complete summary lineage. The marker also applies to a first unsupported known-topic definition and to uncertain values inside that explicit frame. No retroactive interpretation of old turns. The existing typed generic Memory admission path remains separate; broader arbitrary definition validation and contextual corrections remain open. A current owner-valid read still takes priority; ordinary verbatim conversation is not globally rewritten.

Clarification suppression now measures the 24h cooldown from the latest actual delivered unsuppressed question/resolution, not the latest suppressed response; unchanged choices, scope and existing adjacent continuation checks remain required. The 24h duration is an implementation policy, not a new architecture requirement.

## Integrated evidence and operator evaluation

[05E regression suite](../tests/test_mvp05e_memory_acceptance.py) uses real PostgreSQL with deterministic model gateways, existing owner and P0 summary/cognition operation contracts. Covers 50 original turns, six summary operations, exact episodic source preservation, meta-episode support/counterevidence, facts, personal meanings, entity recall, supplied knowledge, MetaMemory and a fresh interpreter read. Separate correction and cooldown cases retain source and owner authority. Reuse the existing [Phase 2 integration](phase2-08.md) tests for durable threads, independent domain updates, contention and selection. No claim of comprehensive semantic quality or model verification. Candidate CI NOT RUN at authoring.

Run with existing disposable PostgreSQL fixture and ordered migrations:
```sh
python -m pytest -q tests/test_mvp05e_memory_acceptance.py tests/test_phase2_acceptance.py
```
Do not substitute focused tests for full Phase 0 and live integration gates.

[05E synthetic scenario](../scenarios/mvp05e_memory_acceptance.json) reuses the existing real-model comparison and blinded review machinery; no fabricated model outputs, automatic rating or private review artifacts. It exercises actual conversational admission/recall, restarts, ambiguity, unresolved corrections and missing knowledge. Host owner admission timing must be inspected before scoring; deterministic full PostgreSQL fixtures separately exercise supplied document and pattern setup, not assumed to happen through ordinary chat.

```sh
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both --thinking off \
  --scenario scenarios/mvp05e_memory_acceptance.json \
  --output-dir artifacts/mvp05e-acceptance-NEW
```
Example model choices only; use installed authorized target model(s), configured disposable .env.e2e and a genuinely new output directory. Full/auto is candidate behavior, model-only diagnostic. Follow [checklist Q](mvp-acceptance-checklist.md#4-q--real-model-conversational-quality-mvp-04) for two independent blinded ratings, adjudication, failure evidence, context/latency and hardware. No live run or human ratings claimed. A08 remains NOT RUN.

## Gate decision and deferred work

05A–D merged; 05E technical consolidation candidate. Full Phase 2 gate requires P2-F cognitive/emotional/attention integration and applicable MVP-07B lineage. Focused MVP-06A can proceed after candidate technical CI; preserve exact approved Goal authority. Source-qualified model use of private documents is a targeted dependency for 06B/06D when necessary; retain read-only information and never admit execution from content. Broad semantic definitions/corrections, cognitive gap discovery (GAP-04/05), relevance feedback and held-out evaluation (GAP-09), episode significance (GAP-12), general claim/repair verification (GAP-07/08) and privacy/deletion propagation remain open. Remote deployment remains blocked by MVP-07A/B. No new model, external provider or scheduler selected.
