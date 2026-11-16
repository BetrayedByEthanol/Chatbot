# MVP-02C.3 — Stage A semantic contracts and E2E diagnostic hooks

**Authority:** Persistent Persona Agent Architecture v2.5.1, §18.1 (CONV-ARCH-01–04)
and §23.1. This increment implements **advisory interfaces and measurement**,
not a new semantic subsystem, persistence owner, or production inference path.

## Scope and trust boundaries

`src/persona_agent/semantic_contracts.py` defines a bounded, source-linked
`SemanticAnnotationBatch` containing zero to four `SemanticClaim` entries.
Each claim identifies:

- **Statement type:** FACT, PREFERENCE, EXPERIENCE, INTENTION, OPINION.
- **Temporal scope:** CURRENT, HABITUAL, PAST_EVENT, PAST_HABITUAL, FUTURE,
  UNSPECIFIED.
- **Modality:** ASSERTED, TENTATIVE, CONDITIONAL, HYPOTHETICAL, UNKNOWN.
- **Pragmatic function:** INFORMING, REQUESTING, CORRECTING, ASKING, UNKNOWN.
- **Proposed topic label:** free-text label, not a stable `DialogueThread`
  identifier. Conversation remains responsible for topic identity.
- **Exact evidence quote** and **interpretation confidence**.

The batch also contains the authenticated source turn UUID, classifier ID,
version, inference route and optional abstention reason. `validate_semantic_evidence()`
rejects cross-turn identity and evidence quotes not present verbatim in the
original source input. **Verbatim grounding is necessary, but insufficient to
verify interpretation accuracy.** Confidence reports classifier output quality
only; it is never a truth score or permission.

`SemanticClassifier` is a runtime-independent Protocol; future Hugging Face,
ONNX, rule-based or LLM adapters must implement the same interface. It does
not require another service, GPU runtime, global mutable state, or schema.

`RuleSemanticClassifier` is a deliberately narrow executable baseline for
six unambiguous grammar patterns and three benign example topics. It abstains
on greetings, complex/multi-claim sentences, third-person claims and unsupported
utterances instead of inventing labels. Its fixed confidence is **uncalibrated**.
It does not duplicate or replace `intent_interpretation.py`, the existing
Conversation-owned intent state, `dialogue_state.py`, the topic resolver,
or P2-01/P2-04 authoritative Memory.

## Evaluation mode

`run_scenario(..., run_semantic_diagnostics=True, semantic_classifier=...)`
and the disposable E2E CLI `--semantic-diagnostics` enable **shadow mode**.
Classification runs *after* the normal foreground response has been delivered.
There is no production hot-path latency, no DB write, and no changed inference
route. Do **not** interpret shadow metrics as production Stage A latency.
The host's real conversation, Memory, Goal and tool execution remain unchanged.

`semantic_diagnostics.json` in the E2E ZIP includes, per turn:

- Source turn UUID, conversation/step, classifier revision and route.
- Claimed types/scopes/modality/pragmatics, topic, source quote, confidence.
- `ANNOTATED` / `ABSTAINED` / `ERROR`, error type and bounded error detail.
- Measured isolated classification latency in milliseconds and
  `SHADOW_AFTER_DELIVERY` stage label.

Scenarios may optionally specify up to four `expect_semantics` partial label
objects (empty list means expected abstention). Validation rejects unknown
labels, forged expected evidence and unbounded lists before provisioning the
E2E database. Contract failures, such as a mismatched source quote or
classifier crash, are reported separately from **behavioral accuracy failures**
against expected labels. Neither counts as approved knowledge.

To run the real-host diagnostics with the configured conversational Ollama model:

```sh
bash scripts/run_persona_e2e.sh \
  --semantic-diagnostics \
  --scenario scenarios/mvp02c3_semantic_diagnostics.json \
  --report artifacts/mvp02c3-semantic-review.zip
```

To exercise the typed contract without PostgreSQL:

```sh
python -m pytest -q tests/test_mvp02c3_semantic_contracts.py
```

The supplied reference classifier and expected labels are **smoke-test baselines**,
not a comprehensive, representative classifier benchmark. The report is synthetic;
the ZIP includes full user text and must not be populated with real user data.

## Deferred, explicitly

- Selecting, downloading, training or deploying Hugging Face models.
- Parallel inference workers, process pools, GPU scheduling, latency SLAs.
- Additional LLM inference, asynchronous routing, or production Stage A
  classification execution.
- Persistence of predictions, topic identity assignment, Memory admission,
  Goal approval, claim graph/correction relationships, classifier calibration.

**Next increment:** benchmark selected models on a reviewed labeled dataset
(including examples requiring abstention), document per-dimension precision,
false correction detection, class imbalance, latency and peak RAM, then
choose a narrowly scoped Stage A production adapter. Only then measure
foreground latency and concurrency against the existing Ollama workload.
