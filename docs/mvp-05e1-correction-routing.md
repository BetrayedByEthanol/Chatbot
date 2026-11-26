# MVP-05E.1 — Correction routing and conversational calibration

Architecture v2.5.1 §§18–19, 20.4. Follow-up to merged 05E; no new architecture, memory schema, goal authority or model selection. PR #125 final head `19b2522148a5a45f317b8fcfbb32e24e48211d2d` passed [PostgreSQL CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45791975798) (2,150 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45791975921). Merged baseline `c6dfac0484c24350d0c9b45e0f39ba4434739d6c`.

## Verified operator benchmark evidence

Source: operator-supplied mvp05e-acceptance-NEW.zip, 26 November 2026; summary and inner bundle SHA-256 verified. Scenario mvp05e-memory-acceptance-v1, merged SHA above, thinking off, one run per configuration. Only aggregate values reproduced; private artifacts, model map, prompts and transcripts are not committed.

| Model | Mode | Mean ms | Median ms | P95 ms | Delivered | Model-routed |
|---|---|---:|---:|---:|---:|---:|
| qwen3.5:9b | full | 7691.04 | 118.53 | 33943.64 | 17 | 8 |
| qwen3.5:9b | model-only | 11718.65 | 9820.00 | 31295.33 | 17 | 17 |
| qwen2.5:7b | full | 3057.59 | 114.91 | 13811.87 | 17 | 8 |
| qwen2.5:7b | model-only | 7305.37 | 5419.80 | 24277.02 | 17 | 17 |

Four completed cases, 68 delivered replies, zero reported runtime errors and zero unapproved goals. Full-mode model routing 8/17 each. Delivered outputs include deterministic host guards even in model-only; no raw model-quality winner. Automated v1 checks consist of delivery and no-unapproved-goal checks, not independent recall correctness. All reviewer templates remain unfilled; no accepted quality winner, target-host threshold or token throughput. Qwen 2.5 7B full is a provisional performance preference on this finite workload, not a binding model choice. The unsupported correction and blocked plan took model calls despite a deterministic outcome on the latter, motivating this increment. Hardware/controlled repeat measurements are not established by this summary.

## Implementation

- Conversation marks a prepared context as REPORT only for a complete recognized unsupported definition with a matching persisted scoped ingress notice, or BLOCKED_ROUTINE when the existing Memory read barrier applies to a supported routine-use request. Historical scope/expiry fence is captured before reads. Full/auto skips inference for these two actions; commit rereads persisted text and current owner state and validates the action before canonical reply. A stale historical dependency retries once; if reconfirmation made the slot eligible, the ordinary planning model path resumes. Replay/selected generation/current turn fences unchanged. No lock spans inference.
- Model-only deliberately still invokes the gateway; commit enforces the same response. Audit event records status and whether a model was invoked; MetaMemory keeps disputed recall metadata. Neither route admits a value, changes a revision, or creates goals/procedures. Unsupported value is not echoed in canonical text. Recognized report acknowledgement says it has not been accepted into memory; blocked routine wording explains the distinction and asks which activities to use for a one-off discussion instead of claiming persistent definition or execution. No new temporary definition store or broad free-form correction resolver. Ordinary user-supplied current-turn data may be discussed under the existing conversational path; no implicit approval. This intentionally does not retrieve the unsupported value from a previous conversation or interpret an arbitrary short reply as authorization.
- Reuse existing validation/corroboration, privacy, expiry, conflict, source and historical fences. Recognized supported reports and eligible routine-use stay on ordinary model path; no compulsory extra classifier.

## Evidence and rerun

New tests in [05E suite](../tests/test_mvp05e_memory_acceptance.py) cover zero-call auto acknowledgement and clarification, forced model output enforcement, replay, owner state unchanged and a real PostgreSQL reconfirmation race that reroutes to normal planning. Scenario validation and evaluator tests distinguish exact canonical reply checks in both modes and auto-only route identifiers. For two-choice identity ordering, accept either exact enumerated order; no arbitrary model-text substring scoring.

Frozen v1 unchanged. New [v2 scenario](../scenarios/mvp05e1_memory_acceptance.json) adds reconfirmation plus exact canonical reply checks for bounded missing fact, personal definition, entity ambiguity/suppression/resolution, unsupported change acknowledgement, blocked routine, restart and missing document. General preference recall, free-form planning and naturalness remain independent review items. Positive document/pattern setup remains covered in existing PostgreSQL integration, not in this natural-chat scenario. Existing post-delivery extraction worker must run; reconfirmation recall must be owner-grounded. Candidate CI, v2 benchmark and independent review NOT RUN at authoring.

```sh
python -m pytest -q tests/test_mvp05e_memory_acceptance.py
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both --thinking off \
  --scenario scenarios/mvp05e1_memory_acceptance.json \
  --output-dir artifacts/mvp05e1-acceptance-NEW
```

Use disposable DB, authorized installed models and new output directory. Compare matched turns and exact model/host settings, not aggregate v1 versus longer v2 as an identical workload. Record actual inference-routed and direct latency; do not claim new measured improvement without a run. Two independent blinded reviews and adjudication remain open. No user-story promotions or Phase 2 exit; focused 06A after candidate technical CI. MVP-07A/B, P2-F and P6 evaluation remain separate.

## Subsequent verification (26 November 2026)

PR #126 merged as `ad221c403918857cb0252cdb56c34420bab6378a`; final head passed [PostgreSQL CI](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45801307151) (2,154 passed, 3 skipped) and [live integration](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/45801307420). Operator-provided v2 archive `mvp05e1-acceptance-NEW.zip` reviewed against the merge SHA: 4/4 completed, 80 delivered, 220/220 automated checks (80 delivery, 80 no unapproved goals, 40 canonical replies, 20 auto-route); all four bundle checksums matched; no runtime errors. Both models reconfirmed the original saved meaning at version 1. Full-mode blocked routine planning approximately 130 ms (Qwen 2.5 7B) and 124 ms (Qwen 3.5 9B), versus approximately 13.81 s / 33.94 s in v1. One run per configuration, not controlled repeat or statistical benchmark. Qwen 2.5 full mean 2.86 s, Qwen 3.5 full mean 4.11 s; 12/20 direct replies in both. All eight independent reviewer scorecards remain unfilled. No model-quality winner or full Phase 2 acceptance. Raw artifacts not committed. Focused 06A proceeds; no further memory optimization gate.
