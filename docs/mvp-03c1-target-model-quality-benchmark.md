# MVP-03C.1 — Target-model conversational quality benchmark

## Scope

This increment measures actual target-model response performance and prepares
the existing MVP-03C natural-dialogue rubric for **independent human rating**.
It is not a claim that natural conversational quality has been accepted.
Architecture v2.5.1 §§18.4 and 22.1–22.3 require brief, context-sensitive
responses, calibrated claims and evaluation without bypassing domain ownership.

The frozen scenario, scenarios/mvp03c_natural_dialogue.json, includes
18 model-routed messages across three conversations and 15 review checkpoints.
Each run uses an isolated, freshly migrated PostgreSQL database and the real
Conversation Manager and Ollama gateway. The baseline model is
**qwen2.5:7b**, not the 0.5B smoke model.

The GitHub Actions workflow, mvp03c1-target-model.yml, runs a bounded CPU
experiment using this model. It records the resolved Ollama SHA-256 model
digest, code revision, migration hashes and a SHA-256 fingerprint of the
private evidence bundle, plus descriptive response metrics. On resource
exhaustion, model timeouts or incomplete contracts the experiment is invalid;
do not substitute a smaller model or report invented quality scores.

**Privacy:** This is a public repository. The Actions workflow deliberately
does not upload the complete conversation ZIP, internal prompts, model-blind
packet, scorecards or individual assistant responses. Only the source-free
numerical summary is allowed in an Actions artifact, retained for seven days.

## Running the benchmark privately

Use the documented isolated E2E PostgreSQL setup from
[mvp-01e-behavioral-evaluation.md](mvp-01e-behavioral-evaluation.md) and a
local Ollama server. Then run:

```bash
ollama pull qwen2.5:7b

python scripts/persona_e2e.py \
  --scenario scenarios/mvp03c_natural_dialogue.json \
  --model qwen2.5:7b \
  --report artifacts/mvp03c1-private-target.zip
```

Save the local Ollama /api/tags JSON response privately, then:

```bash
python scripts/benchmark_target_model.py \
  --bundle artifacts/mvp03c1-private-target.zip \
  --scenario scenarios/mvp03c_natural_dialogue.json \
  --model qwen2.5:7b \
  --ollama-tags artifacts/mvp03c1-private-tags.json \
  --out artifacts/mvp03c1-private-summary.json
```

The evaluator requires a complete, correctly ordered, source-linked
transcript from the exact scenario; 18 delivered non-replayed target-model
responses; valid per-turn response timing; and passing underlying
deterministic contracts. No fake model outputs or test-only Goal approvals
can be substituted. An absent model digest is disclosed explicitly.

The summary includes min/median/p95/max host wall-time, min/median/p95/max
response length, count over 80 words, model digest, scenario and ZIP
fingerprints, and coverage of the 15 qualitative review checkpoints.
These **are not measures of response truthfulness, empathy, or intelligence**.
Host wall time is not streaming time-to-first-token or audio playback time.

## Independently evaluating conversation quality

Only the *private* local ZIP contains the actual replies for reviewers.
Create the existing model-blind packet and **empty** templates:

```bash
python scripts/review_natural_dialogue.py packet \
  --bundle artifacts/mvp03c1-private-target.zip \
  --out artifacts/mvp03c1-private-packet.json

python scripts/review_natural_dialogue.py template \
  --packet artifacts/mvp03c1-private-packet.json \
  --reviewer ACTUAL_REVIEWER_A_ALIAS \
  --out artifacts/mvp03c1-review-a.json

python scripts/review_natural_dialogue.py template \
  --packet artifacts/mvp03c1-private-packet.json \
  --reviewer ACTUAL_REVIEWER_B_ALIAS \
  --out artifacts/mvp03c1-review-b.json
```

Two genuinely distinct people must independently rate all checkpoints
using the existing 0–2 rubric: grounding, continuity, calibration, brevity,
affect, goal support, agency, coherence and repetition. For every negative
rating or critical flag they must cite concrete evidence. Do not assign
model-generated or simulated scores to human reviewers. The human assessment
also examines whether the persona maintains context *naturally*, not just
whether the relevant source was in the prompt.

After both people complete their own review:

```bash
python scripts/review_natural_dialogue.py aggregate \
  --packet artifacts/mvp03c1-private-packet.json \
  --first artifacts/mvp03c1-review-a.json \
  --second artifacts/mvp03c1-review-b.json \
  --out artifacts/mvp03c1-private-human-summary.json
```

A third qualified reviewer should adjudicate disagreements and critical
flags; the decisions and rationale must be recorded separately. Review
results require human interpretation: even agreement on high scores is not
a formal release or authorization decision.

## Acceptance and limitations

- **Target-model run complete** requires a real 7B run and an evidence-checked,
  source-free numerical report with a genuine SHA-256 model digest and exact
  scenario/code identity. CI protocol success does not guarantee quality.
- **Independent quality assessment complete** requires two genuinely
  independent completed human scorecards from the same private packet,
  adjudication where needed, and a reviewed failure analysis.
- **Pending** until those two conditions are actually verified: reported
  natural conversational quality remains unknown. Do not claim success
  from word counts, a simulated test fixture or a small CI model.
- This scenario is a narrow pilot: long-horizon persona consistency,
  multilingual conversation, subjective user satisfaction and streaming
  speech timing remain outside the scope. Do not optimize production
  responses against one narrow numerical benchmark.

No database schema, persistent emotion/relationship state, user-approved
goals, memory facts, tools or production model inference routing are changed.
