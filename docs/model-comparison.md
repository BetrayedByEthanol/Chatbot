# Reproducible persona model comparison (v2.5.1)

Compare **one selected runtime model at a time**, without changing persona,
Goal, Memory or Procedure ownership. This tool runs identical **synthetic**
user turns with a fresh persona database for each model/mode pair and reuses
the existing E2E runner and MVP-03C independent-human review protocol.

## Prerequisites

- Install this repository with `python -m pip install -e '.[test]'`.
- Install Docker and Docker Compose v2, and start Ollama.
- Pull each Ollama model used in the comparison.
- Configure `.env.e2e` using `.env.e2e.example` with a **disposable**
  PostgreSQL password and maintenance DB DSN. The existing
  `scripts/run_persona_e2e.sh` owns a fresh Compose project per run,
  migrates one `persona_e2e_*` database and tears down its own volume only
  after a published ZIP report. Never point at production PostgreSQL.

## One-command comparison

```bash
ollama pull qwen2.5:7b
# Pull the exact Qwen 3.5 tag installed on your machine separately.
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:latest \
  --mode both
```

The mode options are:

- `model-only` (default): same real conversation ingress, model-forced
  reply path, and existing state/workers; not the full adaptive router.
- `full`: send the exact same messages through the ordinary `auto`
  conversation route, including eligible deterministic and tool pathways.
- `both`: run every model once through each mode, with a **new isolated
  persona DB for each model/mode pair**.

A custom scenario must use the repository's validated E2E schema and contain
natural-dialogue `review_case` rubrics:

```bash
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:latest \
  --mode full \
  --scenario scenarios/persona_model_comparison_v1.json \
  --output-dir artifacts/my-comparison
```

The default 25-turn scenario has **three conversation sessions and two
transport restarts**. It tests conversational effort, thinking aloud without
commitment, explicit short-reply preference, uncertain facts and clock
grounding, revised walking intentions, goal-approval boundaries, Nginx
procedure progress, interruptions, and multi-session recall.

The text, session boundaries and human-review rubrics are identical across
models. The only per-mode transformation is `mode=model` versus
`mode=auto`. No model-specific prompt profile is applied by this v1 runner.
Run order is randomized; a private mapping preserves attribution.

## Private output and human review

Each invocation creates a new **0700** directory containing:

- `case_XX.zip`: canonical E2E ZIP, including transcript, outbound model
  requests, timing, dialogue/DB snapshots, intent traces and checks.
- `case_XX.review.json`: blinded chronological human-review packet with
  source-supported failure criteria (no model ID or internal prompts).
- `case_XX.reviewer_a.json`, `case_XX.reviewer_b.json`: unfilled,
  independent scorecards for two reviewers.
- `case_XX.result.json`, `summary.json`: diagnostic counts, observed route
  IDs, mean and median per-turn time, mean reply words, contract status, and
  quality status **AWAITING_INDEPENDENT_HUMAN_REVIEW**.
- `PRIVATE_model_map.json`: model/mode mapping for the operator. Do **not**
  give this or any ZIP/diagnostic output to blinded reviewers.

Only distribute `case_XX.review.json` to raters. The packet contains the
synthetic user/assistant text; the private ZIP includes full synthetic prompts
and state. Do not run real-user transcripts through this fixture.

After two human reviewers fill their scorecards, reuse the existing review
aggregator:

```bash
python scripts/review_natural_dialogue.py aggregate \
  --packet artifacts/my-comparison/case_01.review.json \
  --first artifacts/my-comparison/case_01.reviewer_a.json \
  --second artifacts/my-comparison/case_01.reviewer_b.json \
  --out artifacts/my-comparison/case_01.aggregate.json
```

Repeat for each case; compare independent dimension scores and disagreement
flags before unblinding. Do not automatically declare a best model from
response length or passing database checks.

## Boundaries

The comparison is **sequential**, not parallel. It uses one independently
provisioned database per run and preserves the existing E2E wrapper's
failure-publication retention rules. If E2E publication is unconfirmed, the Compose wrapper returns code 2
regardless of its original runner exit code and retains the database. The
comparison also verifies required ZIP members and CRC integrity, and aborts
remaining runs whenever the bundle is absent or invalid (including code 1).
A status-1 contract failure with a complete published evidence bundle is
retained without a misleading human review packet; other cases can still run.
A failed Compose teardown likewise returns code 2 and stops the comparison.

The `--direct` option uses the existing `scripts/persona_e2e.py` runner
instead of Docker and requires `PERSONA_E2E_ADMIN_DSN` to refer to a
**disposable** PostgreSQL server. It is for developer-controlled test
infrastructure only.

Current Ollama gateway requests do **not** record token usage or exact
quantization, so the summary labels output tokens unavailable. Mean latency
includes host processing and worker overhead, not isolated inference time.
When `auto` chooses a non-model route, the observed route is recorded and
must not be mistaken for an LLM response. Automatic tests validate contracts;
human reviewers evaluate naturalness, grounding and pragmatic understanding.

## Tests

```bash
python -m pytest -q tests/test_model_comparison.py tests/test_mvp03c_natural_review.py
bash -n scripts/compare-persona-models
```

Real Ollama+PostgreSQL comparison remains a manual runtime acceptance test;
these unit tests use no model or Docker daemon.
