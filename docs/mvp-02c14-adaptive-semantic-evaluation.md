# MVP-02C.14 — Adaptive Semantic Interpretation Evaluation

**Status: offline model experiment / source-grounded, read-only.**
No foreground response routing, automatic Memory admission, corrections,
new PostgreSQL tables, durable events, external providers or production NLI
are introduced.

## Reason for this increment

MVP-02C.13's 80-case author-labeled synthetic baseline produced 11
complete current/general preference scopes, 17 missed eligible draft
scopes and zero false scope activations. All 10 paraphrase cases were
missed; 6/7 explicit corrections were missed. These results establish
coverage limitations *on the synthetic author-labeled diagnostic*, not
deployment precision/recall. The original corpus and blind-review packet
remain unchanged. Independent, prediction-blinded review and a separate
unseen holdout are still mandatory before capability or safety approval.

The architecture's **adaptive hybrid inference** means:
preserve deterministic interpretation when adequate; call a model
selectively on unresolved, potentially relevant material; validate its
source, schema and scope without granting mutation authority. This PR
evaluates that design without adding an inference step to normal
conversation.

## Contract-first implementation

`src/persona_agent/semantic_adaptive_evaluation.py` contains:

- `ModelScopeCandidate`: strict, typed, extra-fields-forbidden advisory
  output for Stage A semantic dimensions, canonical identifiers and
  `narrow_current_scope`. A model's positive narrow-scope claim must
  have `PREFERENCE/CURRENT/ASSERTED`, `user`, `general`,
  `enjoys_gardening|enjoys_reading|enjoys_walking`, `yes|no`, and
  `INFORMING|CORRECTING`; inconsistent output is rejected.
- `ModelScopeEnvelope`: exactly `{"candidate": null | ModelScopeCandidate}`.
  Null means the model declines to propose a single supported claim.
- `JsonScopeGateway` protocol and `OllamaScopeGateway`: uses **one**
  explicitly requested native Ollama JSON call, via the existing
  `OllamaChatGateway`; inference remains outside database transactions.
- `interpret_adaptive`: preserves a completed deterministic source-grounded
  scope without a model call; otherwise routes only whole utterances
  containing `gardening`, `reading` or `walking`, under explicit call
  limits. Outputs `DETERMINISTIC` or `MODEL` route with an explicit
  status, non-sensitive reason and latency.
- **Full-turn evidence validation**: the proposed `evidence_quote` must
  match the **ENTIRE source text exactly**, rather than a substring that
  could drop qualification. Invalid/oversized/malformed JSON, unknown
  schema fields, mismatched quotes and inconsistent positive scope flags
  are rejected; model transport failures are recorded separately.
- A **conservative lexical veto** prevents narrow current/general
  activation for many obvious seasonal, place-specific, conditional,
  uncertain, attributed, future/past, compound and quoted sources even
  if a model falsely claims unqualified current scope. This heuristic
  is an *experimental safety bound*, NOT an independently validated
  source-scope checker. It may miss qualifiers and unnecessarily reject
  valid paraphrases. Track `SCOPE_VETOED`, raw model scope proposal
  and final accepted scope separately.

The selected model's confidence is advisory, not calibrated and **never
grants Memory permissions**. The experiment produces only a comparison
report; `NARROW_SCOPE` means a candidate *for offline scoring*, not an
approved domain operation. Future integration still requires authenticated
Conversation input, selected-persona authority, active Memory evidence,
domain-owned conflict/correction validation and independently measured
eligibility.

### Route behavior

```text
Offline corpus text
  -> Existing RuleSemanticClassifier + full-turn Memory grammar
      ├── grounded narrow current preference -> reuse, zero model calls
      └── no supported scope
            ├── no relevant activity cue / unsupported input -> skip
            └── relevant cue + remaining explicit budget -> 1 local model call
                 -> strict schema + exact full-source quote
                     ├── invalid/timeout -> visible rejected/error
                     ├── null/partial -> abstention/partial semantics
                     └── complete scope -> qualifier/attribution risk veto
                           ├── flagged -> no narrow scope
                           └── unflagged -> advisory narrow scope proposal
```

**Caveat:** selective routing here is *diagnostic* (existing activity cue
plus baseline abstention). It is **not** the confirmed future
production adaptive-processing strategy or a general-purpose utterance
importance classifier.

## Running on the local model host

There is deliberately no default model: inference must be opted into.
The script makes bounded calls to the local Ollama `/api/chat` endpoint,
with `format=json`. It will not use a hosted API automatically.

```bash
PYTHONPATH=src python scripts/evaluate_adaptive_semantic_scope.py \
  --suite scenarios/mvp02c13_semantic_scope_corpus.json \
  --model "YOUR_INSTALLED_LOCAL_MODEL" \
  --base-url http://127.0.0.1:11434 \
  --timeout 25 \
  --max-model-calls 80 \
  --report artifacts/mvp02c14-adaptive-semantic.json
```

For a bounded smoke test (clearly reported as **not full corpus**):

```bash
PYTHONPATH=src python scripts/evaluate_adaptive_semantic_scope.py \
  --model "YOUR_INSTALLED_LOCAL_MODEL" \
  --case-ids SC015 SC019 SC025 SC036 SC052 SC066 \
  --max-model-calls 6 \
  --report artifacts/mvp02c14-smoke.json
```

Do not reuse an existing output path; reports are write-once. A model
error or invalid response creates an explicit case result. The script
writes its report, but exits nonzero when such model failures occur.
Neither a network error nor a missing reply is silently counted as
correct.

### What is measured

The versioned report includes source dataset SHA, model ID, prompt
version, draft annotation status, model-call budget, full/subset status,
per-case routes and validation reasons, model latency, and:

- Baseline versus hybrid narrow-scope TP / FP / FN / TN against the
  **same provisional** source labels; cases gained or falsely activated.
- Per-group changes (paraphrases, explicit corrections, uncertainty,
  contextual qualifiers, attribution, compound sources).
- Per-dimension *non-null prediction conditional* supported/correct/
  incorrect/abstained accounting; `null == null` never earns a correct.
- Model attempt counts, invalid outputs, errors, abstentions and source
  qualifier vetoes. Model call overhead is measured separately from
  deterministic classification.

The report deliberately **does not include** raw user text, evidence
quotes, raw model responses or network credentials. The model still
receives the source text for inference, so only use local, authorized
synthetic or otherwise privacy-approved data.

## Tests and review gates

`tests/test_mvp02c14_adaptive_semantic.py` uses scripted, explicitly
non-real model replies to test route selection, budget enforcement, schema
validation, prompt wiring, correction/paraphrase paths, false-scope risk
vetoes, unsupported output and error handling. These are **contract tests**,
not measured model capability or model-result fixtures.

Before any scope extractor is accepted for a live persona:

1. Complete **two independent, prediction-blinded** reviews of the 02C.13
   annotation packet and human adjudication (or create an independently
   sourced review corpus). Distinct IDs and perfect automated agreement
   do not establish independent semantic truth.
2. Create an **untouched, representative holdout**, including compositional
   inputs, correction references, ambiguity, multilingual language,
   privacy-sensitive data and contradictory evidence, without leakage
   from development examples.
3. Compare the *same model/prompt* at fixed decoding/configuration;
   measure true, operational coverage and false scope activation rates,
   correction recall, abstentions, latency, budgets and model failure
   modes. Do not choose a favorable threshold or prompt on the holdout.
4. Only then consider a small, domain-owned **read-only shadow**
   integration. Model proposals must never independently bypass existing
   Memory admission, generation fencing, versioning or authorization.

**Decision register:** model scoring available offline by explicit request;
no production model routing changed; 80 labels provisional; no approved
new Memory extraction capability or automated correction.
