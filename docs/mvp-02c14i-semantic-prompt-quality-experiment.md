# MVP-02C.14I — Semantic classification quality experiment

**Status:** offline, non-authorizing experiment. A prompt candidate is
**not** a model or production routing decision. The authoritative
v2.5.1 design retains deterministic handling of supported simple
messages, selective semantic inference when useful, short foreground
conversations, source grounding, domain ownership, and explicit Memory
admission. None of these contracts change.

## Motivation

14H [real results](mvp-02c14h-semantic-model-failure-diagnostics.md)
showed JSON Schema output enforcement yielded **18/18 technically valid
Qwen2.5:0.5B** outputs, versus **0/18** in unconstrained JSON mode.
Despite that, the schema-constrained predictions matched provisional
author-written labels on only **4/18 statement types** and **1/18
temporal reference sets**. Structural validity is solved for that tiny
test; semantic agreement remains insufficient.

The next useful question is whether a **small, explicitly versioned,
general-purpose semantic instruction** improves draft agreement and
coverage without sacrificing strict output validity. Do not conflate
this with actual accuracy, benchmark overfitting, or production readiness.

## Controlled A/B experiment

Compare `baseline` against `semantic_scope_v1`, **both** using
the exact same local Qwen model digest, full 15-case / 18-reference
claim author-draft suite, fixed JSON Schema, zero-temperature decoding,
350-token generation ceiling, and v14E exact-span evaluation.
The baseline system instruction is byte-identical to 14H and v14E;
original `format="json"` remains the default in all other scripts.

The candidate instruction appends **domain-independent definitions**
and contextual guidance:
- `PREFERENCE` means likes, dislikes or choices, versus factual
  states/routines, personal experiences, intentions and opinions;
- temporal references attach to the **target claim**, not another
  clause. Former states are PAST, current states or habits PRESENT,
  and future events/plans FUTURE, subject to actual text evidence;
- a condition outside the quoted claim may govern the claim,
  whereas negation need not change its statement type;
- missing information must remain `UNKNOWN` or `null` rather
  than being filled from unrelated context.

The instruction includes **no scenario IDs, reference labels, sampled
answers or examples from the existing evaluation set**. It is,
however, a **development hypothesis informed by earlier aggregate
failures** and thus cannot be validated on that same corpus as a
held-out test.

`scripts/evaluate_semantic_prompt_quality.py` executes two
**interleaved** repetitions:
`baseline → semantic_scope_v1 → baseline → semantic_scope_v1`.
This reduces trivial order/temperature effects but does not
establish model stability. It uses 14H's one-code-per-claim
diagnostic accounting, 14E's reference-span classifier and 14F's
immutable Ollama model fingerprint.

Each recorded repetition contains:
- number of technically valid model outputs, errors, safe histogram;
- eligible/reference-unknown, supported/abstained,
  matching/mismatching author-draft counts **per dimension**;
- coverage, supported-only conditional draft agreement, and correct
  draft labels **per eligible**;
- latency, prompt version and content SHA-256, model digest,
  JSON Schema hash, corpus hash, case IDs, repetition index.

The report explicitly says `NO_PROMPT_APPROVED_FOR_PRODUCTION`.
It is **text-free and write-once**: no user sentences, raw
model completions, original errors, per-claim model labels,
exception text or private prompts. Its backend calls are local-only.

### Validation invariants

Reject missing/duplicate/invalid diagnostic events (14H contract);
noninteger or inconsistent error counts; dimensions that claim
support from errored calls; changing eligible/reference-unknown
counts between model variants or repetitions; changing case IDs,
corpus, schema or model digest; invalid repetition bounds; report
overwrite. Parsing or model errors are measured as technical
failures, **not** semantic disagreements. This contract does
not infer a production winner.

## Usage

From repository root, after intentionally installing a local
Ollama Qwen2.5:0.5B model:

```bash
PYTHONPATH=src python scripts/evaluate_semantic_prompt_quality.py \
  --model qwen2.5:0.5b \
  --ollama-url http://127.0.0.1:11434 \
  --repetitions 2 \
  --report evidence/ollama-prompt-quality.json
```

GitHub Actions reuses the same local Ollama service as v14H,
runs both prompt versions, writes the result beside other v14F
evidence and shows a restricted summary (valid outputs and
statement-type/temporal draft matches) in the job UI. A green
workflow means **the experiment executed and its integrity checks
passed**, not that a candidate is better.

## Actual real-model outcome — 19 November 2026

The real [GitHub Actions run 44573149562](https://github.com/BetrayedByEthanol/Chatbot/actions/runs/44573149562)
**passed** and archived artifact ID `14556905186`. Its
`ollama-prompt-quality.json` has raw-file SHA-256
`caca6eabc0ab3cce40f1469785c0b2998eedfe21313830eed4bd4fa073f074be`.
Both variants used **the same** verified local Qwen2.5:0.5B
digest `a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`,
15 synthetic cases, 18 reference claims, identical schema and
generation settings. All **four runs had 18/18 technically valid
outputs**, zero model errors and valid warmups.

**Correct author-draft labels / 18 eligible claims** (unless stated):

| Dimension | Baseline, run 1 | Baseline, run 2 | Candidate, run 1 | Candidate, run 2 |
|---|---:|---:|---:|---:|
| Statement type | **4/18** | **4/18** | 1/18 | 1/18 |
| Certainty | 12/18 | **14/18** | **0/18** | **0/18** |
| Polarity | **18/18** | **18/18** | 17/18 | 17/18 |
| Temporal reference set | 0/18 | 0/18 | 1/18 | 1/18 |
| Conditionality | 0 supported | 0 supported | 0 supported | 0 supported |
| Hypotheticality | 0 supported | 0 supported | 0 supported | 0 supported |
| Habituality | 0 supported | 0 supported | 0 supported | 0 supported |

Both variants emitted temporal sets for all 18 claims, so temporal
results indicate **draft disagreement rather than abstention**.
Baseline supported 17/18 certainty labels; the candidate
**abstained on all 18**. The candidate's small temporal-set
increase (+1/18) is accompanied by a material loss of
statement-type matches (−3/18), certainty coverage (17/18 → 0/18)
and polarity matches (−1/18), reproduced in both runs.
The candidate's total model inference time was about 21.6–22.1 s
per suite, versus 19.8–20.0 s for baseline, on the same CI runner.

**Outcome: the proposed instruction did not improve overall
classification quality on this development dataset. Reject
`semantic_scope_v1` as a promotion candidate.** Do not silently
replace the old prompt or assume the baseline is accurate enough
for Memory-related decisions. In particular, both variants
remain unreliable at temporal-set identification and both
abstain on critical conditional/hypothetical features.

The 14H schema-mode baseline had one temporal-set draft match
on its earlier run, whereas both 14I baseline repetitions had
zero. Temperature zero does not guarantee fully identical
inference outcomes across separate executions. This reinforces
the need for repetitions and independent evaluation.

**Decision 14I-D4 (evidence):** retain the general-purpose
prompt candidate as a versioned **negative experimental result**
and make no production-model or production-prompt selection.
Do not repeatedly tune to these same 15 author-written cases.
Future comparison must use a separately adjudicated evaluation
set and actual intended model sizes/checkpoints; DeBERTa and
Qwen2.5:7B remain unmeasured in this milestone.

## Interpretation

A plausible improvement must show more correct eligible *draft*
labels on both repetitions without reduced output reliability
or a collapse in coverage. Conditional agreement alone is
not sufficient: a classifier can inflate it by abstaining on
hard cases. Aggregate improvements do not identify which claims
changed or prove an improvement in recall of negation, correction,
intent, relational context, or true persona memory behavior.

**Before any deployment consideration:** independently adjudicate
a stratified holdout, use actual intended Qwen and DeBERTa
checkpoints, compare with deterministic and stronger baseline
classifiers, assess errors by claim and dimension without leaking
private data, evaluate full extraction and qualifier/relationship
safety, and measure target hardware foreground latency.
No hidden switching of the active persona, Memory state, Goal,
or Procedure infrastructure is allowed.

### Decision register

- **14I-D1 confirmed:** v14E baseline prompt and production behavior
  remain byte-for-byte unchanged for the default variant.
- **14I-D2 proposed:** candidate prompt is a test-only, versioned
  hypothesis; no automatic production promotion regardless of draft
  agreement changes.
- **14I-D3 confirmed:** report paired case/reference denominators,
  output validity and abstention alongside draft agreement; retain
  repeat-level evidence rather than averaging away instability.
