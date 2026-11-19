# MVP-02C.14H — Local semantic model failure diagnostics

**Purpose:** identify why the real **Qwen2.5:0.5B** v14F evaluation
reported **18/18 backend errors in each of two runs**, instead of
guessing the cause or interpreting failures as wrong semantic predictions.

**Scope:** opt-in, offline, local Ollama, reference-span-conditioned
v14D author-draft suite. Does **not** change foreground routing,
trained model selection, persona state, Memory authorization, or
production inference. Real diagnosis must come from actual execution,
not from the simulated responses in unit tests.

## Baseline evidence and hypotheses

The source is [v14F real CI run 44516887227](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/44516887227)
and the 14G evidence review. The old v14E JSON-mode probe supplied
`format: "json"` and required **all seven exact dimension keys** and
strict enum/bool/null/temporal values. Each failed call was caught by
`evaluate_probe`, mapped to wholly unknown labels, and counted only
as `error_claims`. The archived report did **not** contain exception
types or sanitized stage information. Thus the root cause is
**unverified**: it could be a transport, envelope, malformed content,
field-set, value-validation or truncation failure.

## Implemented diagnostic boundary

`scripts/evaluate_semantic_models.py` preserves its original
`OllamaJsonProbe` JSON default; optional constructor parameters permit
`format_mode="schema"` and a **fixed-code-only** diagnostic sink.
`format_mode="schema"` sends an explicit JSON Schema as the Ollama
chat `format` instead of `"json"`, retaining the **same prompt,
temperature, output limit, parser and strict validation**. This
isolates whether constrained generation changes output validity; it
does NOT establish accuracy, robustness or automatic model preference.

Failure buckets (and only these names) are emitted:

| Stage | Reported category |
|---|---|
| HTTP rejection | `HTTP_4XX`, `HTTP_5XX`, `HTTP_OTHER`, `HTTP_NON_200` |
| Connection/timeout | `TRANSPORT_ERROR`, `TRANSPORT_TIMEOUT` |
| Outer response | `ENVELOPE_OVERSIZE`, `ENVELOPE_JSON_INVALID`, `ENVELOPE_INVALID` |
| Inner model content | `CONTENT_INVALID`, `CONTENT_EMPTY`, `OUTPUT_TRUNCATED`, `CONTENT_JSON_INVALID` |
| Schema contract | `LABEL_NOT_OBJECT`, `LABEL_FIELD_SET_INVALID`, `LABEL_VALUE_INVALID` |
| Successful strict parse | `VALID_PREDICTION` |

No raw source turn, input prompt, model completion, exception text,
HTTP response body, URL or per-claim label is given to the diagnostic
sink. Valid `ProbeLabels` with all dimensions unknown are counted as
**valid structured output with abstention**, not malformed model calls.

`scripts/diagnose_semantic_model_failures.py` evaluates **exactly the
same selected cases** in JSON and schema modes, captures one safe
event per reference claim, and verifies that successful parse event
counts reconcile with v14E model error counts and supported dimension
counts. It captures the locally installed model's immutable Ollama
digest, checks it remains unchanged across modes, and archives
**only** summary failure histograms, aggregate provisional scoring,
case identifiers and a deterministic schema fingerprint. Missing or
duplicated observer events and inconsistent reference denominators
fail closed; model errors themselves are legitimate diagnostic
observations, not a CI infrastructure failure.

The real workflow uses a **local-only** installed Qwen2.5:0.5B model.
It writes `evidence/ollama-failure-diagnostics.json` and displays
a safe warning/category histogram in the GitHub Actions step summary.
Archive retention remains 14 days. The experiment is **not equivalent**
to running Qwen2.5:7B, DeBERTa or a production target machine.

## Reproduce (from repository root)

```bash
python -m pip install -e ".[test]"
export PYTHONPATH=src
# Start Ollama locally, install qwen2.5:0.5b intentionally.
python scripts/diagnose_semantic_model_failures.py \
  --model qwen2.5:0.5b \
  --ollama-url http://127.0.0.1:11434 \
  --modes json schema \
  --report artifacts/mvp02c14h-ollama-failures.json
```

For a quick smoke check, select `--case-ids SD001 SD002`. The
full run uses all 15 synthetic examples / 18 reference claim spans.
The report is write-once; use a fresh filename for each run and
preserve suite hash, schema hash and Ollama model digest.

## Interpretation and next decisions

- If JSON mode reports `LABEL_FIELD_SET_INVALID` or
  `LABEL_VALUE_INVALID` while schema mode produces valid outputs,
  infer a **structured-output compatibility issue** as a likely
  explanation for the prior invalid results. This is evidence, not
  a completed remedy or evidence of semantic correctness.
- If both modes fail with `HTTP_*` or `TRANSPORT_*`, investigate
  service/model/endpoint health and timeout settings, not model semantics.
- If `OUTPUT_TRUNCATED` dominates, examine generation bounds
  separately, without assuming increasing output tokens solves
  semantic quality.
- If both modes return valid outputs, the earlier 18/18 errors are
  **not reproduced** under this setup; compare the immutable model
  digest, Ollama revision, runner, prompt version and input suite.
- Even a 100% valid schema-mode run still requires independent
  adjudicated labels, complete extraction/qualifier/correction tests,
  and target hardware latency measurements before any model
  selection discussion.

**Decision 14H-1:** retain v14E default JSON behavior unchanged;
structured-output schema is diagnostic-only. **Decision 14H-2:**
classify failures by stage using fixed event codes without logging
raw model content; no silent retries, model fallback or Memory writes.
**Decision 14H-3:** reserve root-cause conclusion until real 14H
workflow evidence is inspected.
