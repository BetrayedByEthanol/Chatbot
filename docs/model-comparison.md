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
ollama pull qwen3.5:9b
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both \
  --thinking off
```

The thinking options are:

- `--thinking default` (default): do not send Ollama's `think` field; use each model's configured default.
- `--thinking off`: send `"think": false` to Ollama, including for interpreted/JSON responses.
- `--thinking on`: send `"think": true` to Ollama; requires a model that supports thinking.

The same thinking choice applies to every model/mode pair in one comparison. To compare Qwen 3.5 with thinking on versus off, run the script twice with **different** output directories. The model's `think` option is recorded in the private model map, per-case results, and outbound request evidence; it is not exposed to blind reviewers. No model profile is changed on disk.

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
  --models qwen2.5:7b,qwen3.5:9b \
  --mode full \
  --thinking on \
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
An explicit `--thinking` flag controls the Ollama request setting globally
without altering the scenario, prompt text, or domain-owned state.
Run order is randomized; a private mapping preserves attribution.

## Focused source-backed preference recall acceptance

The frozen 25-turn scenario is unchanged. A separate scenario isolates the
cross-session recall of a *persisted* one-to-two-sentence preference, an
explicit correction to normal length, and an unknown emoji preference:

```bash
scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both \
  --thinking off \
  --scenario scenarios/preference_recall_v1.json \
  --output-dir artifacts/preference-recall-routing
```

The Relationship/Emotion consumer runs at post-delivery boundaries in the
disposable E2E host. Its source-linked preference is available on the next
turn and across sessions/restarts. A deterministic PostgreSQL test checks that
the stored current preference is supplied to the recalled-turn prompt.

The **full/auto** route now handles only a small allowlist of standalone
questions about reply length, planning, and emoji preferences. It reads
authenticated Relationship state and carefully matched first-party planning
instructions, then commits through the existing Conversation response fences.
Unknown saved preferences are reported as unknown, not invented. Ambiguous,
hypothetical, quoted, mixed-intent or unsupported queries remain model-driven.
The `model-only` mode intentionally bypasses this route and continues to
measure each model's interpretation; `both` makes the difference visible
without changing the existing rubric.

**Emoji question semantics:** `What is my current emoji preference?` reads
only the active Relationship projection. `Have I ever specified whether you
should use emojis?` also checks the authenticated Relationship revision
history. It can truthfully distinguish an active preference, a previously
saved preference that was removed, and no *saved record*. The historical
answer does not expose a removed preference's value, and absence of a saved
record does not prove the user never mentioned emojis.

## Wider 25-turn conversational comparison

After landing changes to the narrow recall route, rerun the frozen,
three-session, 25-turn scenario with the **same models and thinking mode**:

```bash
git checkout main
git pull --ff-only

scripts/compare-persona-models \
  --models qwen2.5:7b,qwen3.5:9b \
  --mode both \
  --thinking off \
  --scenario scenarios/persona_model_comparison_v1.json \
  --output-dir artifacts/conversation-post-emoji-history
```

Use a **new** output directory. Compare the full/auto route with
model-only, preserving model attribution in `PRIVATE_model_map.json`.
Inspect all 25 turns, not only the direct preference-recall turn:
grounding of unknown time/car details, walking correction and authorization,
Nginx progress across interruption, brevity, and latency. A PostgreSQL
integration test runs the same frozen conversation with the real workers
and a stub model to verify routing and persistence; that smoke test is not
an Ollama quality benchmark. Real-model outputs still require blinded
independent review.

The `review_case` criteria explicitly flag unsupported recall uncertainty,
failure to give the precise sentence limit and its exception, reliance on
superseded preferences after a correction, and fabricated emoji preferences.
The scenario **does not** automatically score answer quality from text or
declare a model winner; use the blinded `case_XX.review.json` packets and
independent human scoring.

When diagnosing a model claiming it cannot remember, inspect both the
`Conversation grounding` section and the `Scoped persona presentation context`
section in the private outbound request evidence. The former covers only
current-session quotes and must never imply that saved Relationship state is
absent. The latter is a trusted authenticated source for the user's explicit
communication preference. Do not distribute full prompts as blinded reviews.

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

Only distribute `case_XX.review.json` and the matching empty scorecard to each rater. The packet contains the
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

### MVP-04A — Review and evidence integrity

Validation now names the reviewer pseudonym, case, dimension or offending
critical code. Scores must be integer 0/1/2 (not null or bool); notes are
required for any negative score or critical flag. Allowed critical codes:
`fabricated_history`, `unauthorized_action_claim`, `unjustified_commitment`,
`unsafe_affect_claim`, `ignored_explicit_boundary`. Descriptive rubric text
belongs in notes; never drop genuine critical evidence just to pass validation.

Inspect an existing comparison without rerunning Ollama, modifying scorecards,
or trusting a saved aggregate:

```bash
python scripts/review_natural_dialogue.py status \
  --directory artifacts/conversation-post-emoji-history \
  --out artifacts/conversation-post-emoji-history/review-status-01.json
```

The private, create-only report includes each planned case (including NOT_RUN),
run/packet mismatches, reviewer-slot validity, agreement and disagreement/flag
counts for valid pairs. Successful CLI execution is **not** quality acceptance.
Reviewer independence and adjudication still require actual humans.
New comparison summaries include bundle SHA-256, observed model/scenario,
host-response nearest-rank p95 and timed-message count, bounded failure
classifications and unstarted cases. Older v1 comparisons are supported but
their missing original bundle hash is explicitly labeled; the report compares
recorded outcomes, attribution and regenerated packet digest, not a proof of
original artifact authenticity. It refuses a replacement bundle inconsistent
with the original recorded outcome. Keep reruns in new evidence directories;
do not overwrite old case ZIPs or rewrite the original summary to force a pass.

The Ollama adapter retains the same ModelUnavailable failure boundary and
exception cause but distinguishes timeout, HTTP status, refusal/reset, DNS,
HTTP protocol and other transport failures using bounded messages. Existing
E2E errors preserve those messages and step index; summaries retain only safe
classifications, never provider body/URL or arbitrary exception message.
Old generic gateway failures remain unclassified; no diagnosis is invented
from a replacement trace. No retries, automatic provider fallback, authority
changes, benchmark text changes or extra model inference are introduced.

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

### MVP-04B — Grounding and response presentation

Private archived traces at runtime revision `9c2ce5c` show that the direct
clock question with a verification qualifier went through the model in both
modes. The 7B model claimed a specific time without a clock result, and
invented a vehicle in the next personal-fact question. The 9B model-only
reply skipped a missing Nginx configuration prerequisite. Casual-chat replies
sometimes introduced large menus or extra choices after the user signaled
low effort. The current full-route preference recall already uses the
source-backed deterministic path; retain it and its exact sentence-limit
exceptions.

MVP-04B extends only the bounded direct clock grammar, adds explicit
personal-fact grounding and narrowly enables the existing scoped Memory
retrieval for named personal facts, and strengthens the existing presentation
prompt. The forced model-only path remains a model path and receives a
current-time evidence reminder rather than a fabricated result. No new
model pass, retrieval subsystem, task authority, new procedure or reply
truncation policy. Personal-fact recall uses current eligible sources and
does not guess from a question, assistant assertion or missing context.
New retrieval is limited to already eligible normal-privacy Memory records;
broader raw historical retrieval and entity resolution remain later work.

The deterministic clock route is directly testable; the other prompt changes
are behavioral guidance, not a proof that a model cannot hallucinate or skip
a step. Re-evaluate both models using the unchanged frozen scenario and
held-out examples, with independent review and source-qualified findings.
Keep case 02's mismatched replacement evidence separate; do not repair
historical summary metadata or alter reviewer scores. No real-model
acceptance or quality winner is established by these code changes.

### MVP-04C — Current-turn pragmatics and dialogue repair (first increment)

The MVP-04B archive (runtime `5a2195b`) contains four technically complete
runs. In each, a low-decision observation elicited another choice or question.
The forced 7B model path still fabricated clock and vehicle details despite
the new prompt guidance. An explicit pause followed by a named topic switch
also missed the narrow switch parser. These findings are diagnostics, not
independent human ratings.

This increment recognizes bounded first-party low-decision, chat-only,
no-question, no-goal and undecided cues. It adds current-turn guidance and
a selective post-model safeguard at the existing fenced response commit.
Compliant replies are unchanged; a directly conflicting choice, agenda,
commitment or detectable unverified clock claim can be replaced by a short
acknowledgment or limitation. The adjustment records a reason and policy
revision in `conversation.response.pragmatic_adjusted.v1`, correlated with
the input event. The model ID remains the actual generator; private raw model
evidence and delivered responses can differ. The forced model-only route
continues to make its primary model call and uses the same host safeguards;
it is not unrestricted raw model evaluation. No added LLM pass, new tool,
persistent mood/preference, task, or goal authority. The clock guard does not
apply to the trusted clock result path.

Explicit pause-and-switch syntax now uses existing topic navigation and
delivery fencing. Recognized pragmatic cues and quoted input cannot resolve
an unrelated automatic pending question; indecision and explicit refusal
produce discussion-level observations or instructions rather than objective
commitments. The prior question is preserved, not cancelled, and operation
state is untouched. A genuine later answer still uses existing dialogue
resolution rules.

Limits: narrow English grammars and detectable reply patterns only; no
universal verifier, sentence-count enforcement, durable no-question policy,
automatic goal admission or intentional silence. General personal-fact
verification, ambiguous reference repair, sarcasm calibration, D&D/weather
owner-authority scenarios and full procedure progress remain outstanding.
Do not equate passing deterministic tests with real-model naturalness.
The frozen comparison and review protocol are unchanged. Re-run on a new
directory after merging; inspect both raw model output and delivered replies,
including held-out examples, and retain independent review requirements.

## Tests

```bash
python -m pytest -q tests/test_model_comparison.py tests/test_mvp03c_natural_review.py tests/test_phase1_model_gateway.py
bash -n scripts/compare-persona-models
```

Real Ollama+PostgreSQL comparison remains a manual runtime acceptance test;
these unit tests use no model or Docker daemon.
