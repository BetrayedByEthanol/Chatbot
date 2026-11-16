# MVP-01F.1 — Behavioral Acceptance Tests & Interpretation Diagnostics

**Scope:** evidence and test hardening only. This increment does **not** change
conversation routing heuristics, enlarge tool privileges, introduce another
inference pass, approve goals, or fix all currently observed behavior. It
turns the MVP-01E human-reviewed failures into reproducible assertions and
records enough evidence to distinguish model omission from runtime rejection.

## Motivation and actual regression evidence

The uploaded first-run `mvp01e-review.zip` executed 28 turns with
`qwen2.5:7b`. The runner reported 62/62 contract checks passing despite the
following observed behavior:

- "I've been thinking about walking more regularly." received a natural
  answer but produced no persisted `OBJECTIVE` signal or walking thread.
- "Actually, twice per week would be better." yielded `CORRECTION` with no
  linked objective.
- "Switch the topic to cooking." focused cooking; "What about that walking
  goal?" later did not restore walking focus.
- "Can you run this for me?" and the hypothetical data-deletion question
  prompted unsupported capability claims. No unauthorized operation ran.
- Cross-session walking recall invented prior details; long-term recall is
  **MVP-02** and is not graded as MVP-01 cross-session retrieval.

A successful response-delivery contract is not proof of understanding.

## Acceptance contracts

Scenario JSON now supports these optional, validated per-message keys:

| Key | Check | Score |
| --- | --- | --- |
| `expect_intent` | A persisted user-turn intent has the required kind (or no signal for null). | Behavioral |
| `expect_related_intent` | An intent from this turn links via `related_signal_id` to a persisted signal of the expected kind. | Behavioral |
| `expect_focus_contains` | The active dialogue's focused thread title contains the expected case-insensitive topic cue. | Behavioral |
| `expect_no_new_operations` | No new operations were admitted by the current input; compared with the preceding state checkpoint. | Behavioral |
| `review_response` | A human reviewer checks a specific semantic claim in the actual reply. | **Unscored, pending** |
| `expect_model_id` | Exact supported registered tool/response route. | Contract |

`response_delivered` and `no_unapproved_goal` remain contract checks.
All checks use actual persisted database snapshots and the dialogue domain
loader; model-written text never fabricates pass evidence. Existing
idempotency, provenance and cross-tenant observation boundaries stay intact.

The 28-turn scenario now declares objective, correction/reference and topic
expectations at the demonstrated failure points; it flags execution and
deletion claims for explicit human review. These are intentionally **red
behavioral checks** until MVP-01F.2 resolves the underlying behavior. The
CLI may exit 1 while still producing a complete successful integration report.

## Interpretation diagnostics — one inference pass

`OllamaChatGateway` accepts an optional observational `response_observer`;
the evaluation-only `RecordingGateway` installs it on an immutable clone,
alongside the existing *exact outbound HTTP request* observer.

The response callback receives the validated **raw response text**, before
`generate_interpreted()` parses the JSON envelope. It records the actual
model response (synthetic tests only), schema status, typed candidate if valid,
source-turn ID, shared production-admission gate result, and eventual persisted
signal outcome. Key examples:

- `INTENT_NULL`: model explicitly emitted a null intent.
- `INTENT_SCHEMA_REJECTED`: metadata did not pass the same Pydantic shape.
- `BELOW_CONFIDENCE_THRESHOLD`, `QUOTE_NOT_GROUNDED`,
  `MODEL_INSTRUCTION_DISALLOWED`: production admission guard rejected it.
- `PREEXISTING_SIGNAL`: deterministic ingress already recorded a cue.
- `MODEL_SIGNAL_PERSISTED`: evidence-linked model proposal was committed.
- `VALID_BUT_NOT_PERSISTED`: investigation required; no fabricated success.
- `RAW_RESPONSE_UNAVAILABLE`: deterministic/fake gateway or failed request
  supplied no response observation; do not guess the model output.

The pure `diagnose_model_candidate_gate` function is shared by runtime
admission and observation to avoid divergence. Observers do not write
PostgreSQL, authorize actions, retry inference, or affect user-facing replies.
Only bounded, synthetic state observations are packaged.

## Reporting

`interpretation_diagnostics.json` contains the model-proposal chain for
eligible turns, including those with a missing or invalid proposal.
`review_items.json` contains human-review prompts and actual replies.
`evaluation.json` and `manifest.json` distinguish:

- `contract_passed`: deterministic persistence/delivery/authorization
  constraints (no execution errors);
- `behavioral_checks_passed`: deterministic expectations for intent,
  references, focused topic and operation counts (no execution errors);
- `behavioral_accepted: null`: never auto-declare semantic acceptance.

The top-level `passed` is the conjunction of deterministic checks and no
execution errors. Human review **remains mandatory**. A false behavioral
check must not be silently reported as a contract regression or automatic
semantic approval. The ZIP is still published before database teardown.

## Verification

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/phase0
python -m pytest -q tests/test_mvp01f1_acceptance_diagnostics.py
python -m pytest -q tests/test_mvp01e_behavioral_evaluation.py
```

Additional tests compare the annotated scenario with the real authenticated
PostgreSQL host using a one-call mocked Ollama HTTP response. They cover
null/schema-rejected/ungrounded/accepted candidate provenance, a deliberately
missing objective that fails behavioral (not contract) acceptance, expected
focus/linkage, operation-count nonmutation, review isolation and ZIP export.

The existing complete PostgreSQL suite and real-Ollama CI must also pass.
To collect a new human-review report:

```bash
bash scripts/run_persona_e2e.sh --report artifacts/mvp01f1-review.zip
```

No auto-install of Ollama models is added. Install the configured model
manually if it is missing. Upload the ZIP, review failing checks with
`interpretation_diagnostics.json`, and implement **MVP-01F.2** as a separate
reviewable behavior-change PR.
