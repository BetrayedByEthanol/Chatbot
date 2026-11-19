# MVP-02C.14F — Real semantic model benchmarking

## Scope and evidence status

This increment executes **real local backends** using the existing MVP-02C.14E
reference-span-conditioned classifier, not injected model output. It measures
inference runtime and provisional-label agreement without promoting any
classifier to production. It does not assess autonomous claim extraction,
multi-turn correction targets, qualifier recall, relationship transitions,
Memory admission, or emotionally aware conversation quality.

**The 15 synthetic SD001–SD015 labels are author drafts**; they are NOT
independently reviewed, and the target claim quotes are supplied from the
reference to every backend. Scores must be described as *conditional draft
agreement*, not accuracy. Real model runs are useful engineering measurements,
not approval to deploy.

## Reusable tools

- `scripts/evaluate_semantic_models.py` evaluates real local spaCy cue
  baseline, local DeBERTa zero-shot NLI, and Ollama JSON prediction.
- `scripts/compare_semantic_model_reports.py` enforces same suite hash,
  prompt version, case IDs, claim counts and different backend IDs.
- `scripts/benchmark_semantic_models.py` adds model construction timing,
  warm-up, repeated evaluations, suite-time distributions, process RSS,
  hardware and library versions, local model fingerprint and write-once,
  **text-free** evidence. It writes one standard v14E report per backend for
  paired comparison and one v14F repetition/provenance profile.

## Real CI smoke benchmark

`.github/workflows/mvp02c14f-semantic-benchmark.yml` executes a genuine
spaCy tokenizer/cue baseline and **Qwen2.5:0.5b**, served from local Ollama,
on the same full 15-case provisional corpus, with 1 warm-up and 2 measured
repetitions. This is a **small Qwen 0.5B diagnostic**, NOT a surrogate for
Qwen2.5:7B. It records an immutable Ollama model digest and source suite
SHA-256; reports contain no user/source text or raw completions.

The workflow can run on PR changes to the benchmark code, or be manually
dispatched. Open its completed run in GitHub Actions and inspect the retained
artifact `mvp02c14f-real-spacy-qwen-benchmark`:

- `spacy-scores.json` and `qwen-scores.json`: the actual first measured
  run scored by the v14E evaluator.
- `spacy-profile.json` and `qwen-profile.json`: per-run errors, dimensions,
  warm-up timings, median/p95 full-suite timing, versions and fingerprints.
- `spacy-qwen-paired.json`: head-to-head diagnostic agreement on matched
  source spans and exactly the same authored cases.

The GitHub runner CPU and Ollama server can contend for resources. Python
RSS does **not** include the separate Ollama container. Wall-clock timings
include JSON transport and token generation, but not initial model startup
outside the measured construction/warmup windows. Report host characteristics
and repeat on the target hardware before making any latency decision.

## Local three-backend benchmark

On an authorized machine where a local DeBERTa NLI checkpoint is *already*
available and Ollama has the intended model installed:

```bash
python -m pip install -e ".[test,semantic-eval,zeroshot]"
export PYTHONPATH=src OMP_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# Run from a fresh empty directory for write-once files.
mkdir -p artifacts/semantic-14f
python scripts/benchmark_semantic_models.py --backend spacy \
  --warmups 1 --repetitions 2 \
  --report artifacts/semantic-14f/spacy.json \
  --profile artifacts/semantic-14f/spacy-profile.json

python scripts/benchmark_semantic_models.py --backend deberta \
  --model-dir /models/nli-deberta-v3-base --device cpu \
  --min-score 0.65 --min-margin 0.10 \
  --warmups 1 --repetitions 2 \
  --report artifacts/semantic-14f/deberta.json \
  --profile artifacts/semantic-14f/deberta-profile.json

python scripts/benchmark_semantic_models.py --backend ollama \
  --model qwen2.5:7b --ollama-url http://127.0.0.1:11434 \
  --warmups 1 --repetitions 2 \
  --report artifacts/semantic-14f/qwen7b.json \
  --profile artifacts/semantic-14f/qwen7b-profile.json

python scripts/compare_semantic_model_reports.py \
  --reports artifacts/semantic-14f/spacy.json \
            artifacts/semantic-14f/deberta.json \
            artifacts/semantic-14f/qwen7b.json \
  --output artifacts/semantic-14f/paired.json
```

This does **not download** HF weights. DeBERTa evaluation must remain
**unmeasured** until actual local model files are available. Do not substitute
previous v14B/C experiments for the v14F structured dimension scores.

## Required decision gates

1. Verify the workflow and artifact contain actual model runs rather than
   injected tests or missing-model abstentions. Record all missing/invalid
   predictions and do not hide them in an aggregate accuracy number.
2. Review CPU resource contention and compare both conditional draft
   agreement and overall coverage; a high agreement with near-zero support
   is not success.
3. Obtain an independently adjudicated holdout with multi-claim, multi-turn,
   temporal, negation, conditional, correction cue/target and privacy cases.
4. Benchmark **the production-intended model version and hardware**;
   distinguish cold startup, steady state and foreground latency budgets.
5. Assess claim extraction and source grounding separately using the v14D
   contracts and targeted Memory admission/correction safety tests.
6. Any production routing needs an explicit subsequent architecture decision
   and a separate PR; no benchmark grants Memory mutation authority.

**Decision:** 14F collects reproducible actual-model evidence, not a
new production model-selection, training or runtime routing policy.
