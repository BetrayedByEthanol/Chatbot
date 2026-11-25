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
Compliant replies are unchanged; list formatting alone is not evidence of
an agenda or choice. Require an explicit directive, user-facing choice menu
or proposed agenda; goal refusal does not by itself prohibit unrelated
conversational choices. Explicit conditional agreement language (e.g. "until
we've agreed") is not an asserted commitment; the exemption applies only to
the matching agreement clause, not later independent claims or promises.
Agreement detection also requires a recognized affirmative goal/plan object
or concrete scheduled action; agreements to defer or discuss and ambiguous
agreement wording are preserved. Explicit UTC/GMT signed numeric offsets are
timezone descriptors, not clock readings; preserve offset clarifications
while still checking adjacent time values and tool-check claims.
A directly conflicting choice, agenda,
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

### MVP-04C.2 — Contextual replies and correction authority

A concrete ingress defect remained after 04C.1: adjacency and absence of
competing acts were sufficient to close an automatic QUESTION even if "yes"
answered a "which day?" question, or a bare reference/uncertainty did not
answer it. The new bounded compatibility gate resolves only supported polar
short replies against a unique adjacent delivered automatic QUESTION;
indirect questions, negative/compound questions and alternatives abstain.
The existing longer free-text path is unchanged; automatic DECISION and
explicit caller-owned acts remain outside this gate. A pending-act context
view uses scoped response delivery and source sequence to distinguish
adjacent, stale and competing acts. It asks the primary model to clarify
only when needed rather than assuming approval. No second inference or
new state store.

For PPA-US-105/PERS-ALIGN-01, explicit recorded-state change requests and
direct observations receive bounded intent signals and source/authority
guidance. Correction claims, instructions and hypotheticals are not
permission to alter the state. A complete bounded integer addition,
subtraction or multiplication equation can be checked deterministically;
the result does not validate its operands, rules or source provenance.
The existing Memory owner amendment test exercises the actual separation
between conversational correction and versioned authoritative revision.

C15/C16 input examples are tested as context delivery, not real Game/Weather
execution: neither capability is registered. No fabricated game-state
owner, weather observation, owner write or generic authorization layer.
The actual accepted game-roll/rules correction and API discrepancy owner
integration are still outstanding. Source-backed personal-fact output
verification, general semantic reference resolution and sarcasm likewise
remain open. Existing Memory, preference and pragmatic output safeguards
are unchanged. No new status promotion, no benchmark scenario changes.

### MVP-04C.3 — Exact saved personal-fact grounding

The 04B archive demonstrated that prompt-only guidance did not prevent the
7B model from inventing a vehicle. For three supported standalone relations
(car driven, phone used, camera owned), auto mode now uses
`conversation.personal_fact_recall.v1` instead of a model call. Model-only
still runs its original inference but the same fenced commit renders the
answer from current eligible Memory evidence, not generated prose. This is
canonical presentation of a narrowly specified saved fact, not a general
semantic verifier. Other questions retain model routing; advice, different
relations, quotations and mixed requests abstain.

The Memory-owned exact FACT/user/predicate slot is read in the short response
commit after selection/current-turn/revision checks. Existing slot revisions
are share-locked before reading the source and conflict eligibility. Missing,
expired, retracted, non-normal, future-source and unsupported-source records
produce a bounded unknown answer. Newer unresolved extracted conflicts
produce a disputed answer without either value. The integration conflict
fixture seeds the existing candidate schema directly because device/vehicle
extraction is not enabled; it does not claim real extraction coverage.
A supported source must be a
complete first-person assertion of the requested relation and value, not a
substring from a qualified, quoted or historical claim. A known answer is
explicitly the last saved user report, not independent verification or a
claim about the current physical world. Recall never writes Memory.

The source-correlated `conversation.response.personal_fact_grounded.v1`
event records status and policy revision; known responses include Memory
identity/version/source turn, but unknown/disputed replies omit these
references. Model-only records whether canonical rendering replaced its
reply. No raw values are added to audit payloads. The frozen scenario is
unchanged; its auto smoke now expects 22 model calls. Model-only is still
host-guarded, so inspect raw provider output separately.

Limitations: three supported relations, exact slot keys, bounded English
source grammar and existing Memory admission/privacy rules. No automatic
device/vehicle extraction, raw-transcript recovery, semantic entity
resolution, or detection of every unconsolidated correction. A missing
eligible saved record does not mean the user never said it; a known
response describes the saved report. Broader recall and correction lineage
remain MVP-05B/C work. No new acceptance promotion.

### MVP-04D — Responsive tool and terminal outcome paths

The existing P0 scheduler, read-only operation admission, persisted UUID
correlation and explicit delivery acknowledgment remain authoritative. The
registered immediate clock and asynchronous text-statistics paths now use a
minimal authenticated response snapshot (current turn, previous user input
for capability-claim enforcement, selection generation and conversation
revision). They do not assemble optional model/Memory/affect/procedure
context or perform LLM inference. The clock samples after that snapshot;
temporal locale/presence policy remains MVP-05A.

The foreground wait parameter is checked before durable admission. It
bounds polling/sleep, not database/network round-trip latency or a hard
end-to-end SLA. A pending notice describes admitted work, not a running
provider or promised success. Retry-wait and cancellation-requested
operations remain pending. Confirmed successful completion still requires
the matching operation result reference and attempt. FAILED/CANCELLED
produce fixed, non-sensitive terminal notices. An inconsistent COMPLETED
record with missing/mismatched attempt evidence produces an unavailable
notice, not success. If already terminal, the initial direct reply carries
the outcome; otherwise the existing ready/deferred/suppressed and initial
delivery checks apply to the terminal notice. No new operation retries or
effect authority are introduced.

The follow-up event records outcome separately from delivery state; the
acknowledged text is persisted as assistant context, and replay reads that
exact delivered text instead of reconstructing it from worker state.
Preparation rechecks the call after locking selection and conversation.
Transport still owns send/ACK; this is not exactly-once delivery across an
external channel crash or proof of user comprehension.

Regression coverage includes real retry exhaustion, owner cancellation,
missing attempt evidence, invalid wait input, blank output, direct context
isolation, restart/replay, and full new-turn response plus acknowledgment
while a worker is blocked. Existing tool tests retain successful direct and
deferred execution, authorization, restart and relevance checks. No schema
migration, new tool/provider or intentional-silence policy. No numeric
latency threshold or quality acceptance promotion. Measure the actual host
latency and queue/resource behavior separately on target hardware.

### MVP-05A — Trusted temporal context

Conversation captures a typed receipt-time snapshot in the existing ingress
transaction, keyed by the authenticated turn. Migration 0043 adds nullable
JSONB and a scoped temporal index; old inputs are not assigned invented
timezones. The database-generated receipt instant, explicit host-provided
IANA zone (or unknown), local offset, earliest recorded input, previous
recorded input and last earlier input in another conversation are distinct.
The latter is a prior-session interaction anchor, not an observed session
end or logout. Only source-linked primary-user turns count; callbacks,
assistant output, timers and notification policies cannot establish presence.

The same immutable snapshot accompanies full and minimal response contexts.
Host timezone metadata is optional and not inferred from user text, device
location or quiet-hours policy. Existing UTC-labeled clock fallback remains
available without pretending UTC is the user's timezone. New auto-routed
clock requests use their persisted zone or UTC, including a pending-turn
retry with a different host default; the older default argument is retained
for pre-migration turns. An explicit zone applies to that input; changes
apply to subsequent new turns, never replay or old date resolution. The metadata follows the existing ingress
idempotency convention (not part of the command fingerprint).
Legacy rows get a qualified read-only snapshot with unknown timezone.

Complete standalone relative expressions today/tomorrow/yesterday,
24-hour HH:MM local wall times, simple date questions and bounded
"in N minutes/hours" durations receive typed, persisted resolution. Local
dates do not invent midnight; durations use UTC elapsed time. DST gaps and
folds abstain rather than rolling forward or choosing an offset. General
natural-language scheduling, relative dates embedded in instructions,
calendar policies and arbitrary date ranges are not implemented here.
A resolved timestamp is evidence, never an approved notification,
Goal deadline, operation admission or presence transition.

Only temporal cues and short greetings receive bounded prompt context;
ordinary model requests do not acquire temporal prose. Every new input
performs one scoped indexed anchor query; target-host query cost and
long-session selectivity need measurement. The snapshot reflects prior
committed visible inputs, not a total order across concurrent sessions.
Earliest recorded contact is not proof of actual first acquaintance.
Tests cover DST, timezone change, unknown zone, elapsed instants, legacy
reads, scoped history, restart/replay and clock routing. No new acceptance
promotion or frozen scenario modifications.

### MVP-05B.1 — Shared historical eligibility and correction precedence

Hybrid/focused retrieval previously omitted the newer-conflict exclusion
already used by direct Memory recall. Shared Memory-owned eligibility now
applies to direct, exact-slot and hybrid reads. Conflicts compare source
chronology and value to the current revision, not the candidate target
version. Focused recall supplies a topical uncertainty notice for visible
disputes; accepted corrections remain owner-versioned.

Exclude episodes anchored to a superseded/ineligible Memory revision or a
deterministic candidate source whose target is ineligible or disagrees,
before ranking/limits in hybrid and topical episode search. This is exact
turn-level lineage, not semantic claim matching. A multi-topic source may
lose an otherwise useful episode; original turns and episodes remain
stored. Direct historical source inspection remains a separate existing
contract. Full derived-data lineage and deletion/summary reconciliation
remain MVP-07B work.

Current eligible Memory values precede optional summaries and interpreted
episodes in the existing context assembly; repeated identical recall
budgets reuse their reads within that assembly. Owner values are
indivisible under retrieval/rendering budgets: skip an oversized value,
never truncate a qualification. Episode descriptions retain bounded
abridgment. Historical fallback reserves the adjacent cadence anchor and
latest eligible correction together; omit the anchor if both cannot fit.

PostgreSQL regressions exercise disputed/current revisions, focused recall,
episode suppression with retained source, expiry/privacy/retraction and
exact-value budgets. Extend the existing tight correction-budget test.
No new owner, schema, model call, vector service, or acceptance promotion.
General semantic matching of unconsolidated corrections, date-expression
routing, general historical model-context revision fencing and full
summary/derived-store lineage remain open. Explicit API date bounds are
unchanged. This is a bounded read-contract increment, not a general factual
output verifier.

### MVP-05B.2 — Bounded dated historical retrieval

Recognize standalone "What did I/we say/tell you/discuss/mention about
TOPIC PERIOD?" questions with one supported calendar period. Resolve
today/yesterday, ISO local date, last week/month, 1–99 days ago and 1–52
weeks ago (English one–twelve or numeric) against the original trusted
receipt and explicit IANA timezone. N weeks ago is the Monday–Sunday
local calendar week containing the date N weeks before receipt. Label
that interpretation, use half-open UTC boundaries (including 23/25-hour
DST days), and cap at receipt. Missing timezone, multiple/unsupported
periods or ambiguous local-midnight boundaries produce clarification
guidance, not an undated substitute. Recognizable but unsupported weekday,
named-date and other detected period expressions within this question
family also abstain. Other question families are unchanged.

Reuse the existing scoped hybrid candidate query with date constraints
before ranking/limits, plus a bounded FTS query for original authenticated
responded user turns across conversations, including the current one.
Conjunctive topic terms are parameterized; current eligible owner revisions
and original quotes outrank model-interpreted episodes. Exclude raw sources
already governed by Memory revisions/candidates and apply existing raw
privacy safeguards, including to original episode sources. Multiple
projections of one source are not independent corroboration. Preserve
whole quotations and owner values; bounded evidence is not proof of
uniqueness, and competing matches require clarification if the referent
remains unclear. The dates refer to the source report, not necessarily
the event date or a current fact.

Recognized dated requests skip undated Memory, summary, episode, focused
topic and historical correction-context retrieval. Recent verbatim
dialogue remains normal context, not evidence for the requested period.
No new model call, owner, schema, general temporal parser or state mutation.
The result is model-facing evidence, not an output verifier. Existing
privacy, correction and retraction rules still apply: a corrected older
revision is not exposed as historical fact, even for a dated request.
This is not as-of revision reconstruction. Other date question forms,
semantic retrieval, complex date ranges, full entity ambiguity, and
unconsolidated corrections/context fencing remain outstanding. The bounded
candidate view can miss valid older evidence; never assert uniqueness.
No acceptance promotion or frozen benchmark changes.

### MVP-05B.3 — Immediate report precedence and historical response fencing

Migration 0044 adds a source-linked explicit report projection, populated
atomically with authenticated input by the existing positive-allowlist
Memory grammar. A newer differing exact-slot report suppresses the old
saved value before asynchronous consolidation, without admitting a
candidate, amending the owner record, or granting authority. Both values
remain withheld from authoritative recall until resolved by the existing
owner workflow. Raw turn and episode recall also exclude superseded or
disputed exact-slot projections; the original turns remain stored.
Unowned, non-superseded reports can still be historical quotations.
The projection is prospective: no automatic reinterpretation of legacy
turns, expanded safe grammar, or general semantic correction matching.

A scoped historical revision covers changes to Memory records, extraction
candidates, explicit reports, episodes and summaries. Deferred constraint
triggers advance it atomically at writer commit, after domain row locks
and event writes. New persona instances initialize their revision row.
Explicit recall/summary/focused history paths observe the revision before
assembling context and retain the nearest future normal active expiry.
The existing response commit rechecks the revision under a short shared
lock and checks expiry against database time. No Memory row locks are
acquired after this scope fence. No transaction or lock spans inference.
A stale historical model reply is discarded; the wrapper retries once
with fresh context, then leaves the input pending on further conflicts.
Other stale-turn/selection/procedure errors and model failures are not
automatically retried. Direct Relationship/personal-fact routes retain
their existing owner-specific fencing/commit-time rendering.

This is deliberately a coarse persona-scoped dependency: even an
unrelated tracked slot change can invalidate an explicit recall. Routine
conversation and direct tools do not read or acquire this fence. The
tracked population does not cover every raw-transcript/intent change,
ordinary summary compression, derived semantic lineage or all external
delivery races; revision revalidation occurs at response commit, not
after transport delivery. Existing persisted responses remain replayable.
No new model classifier, scheduler, Memory mutation authority or general
factual-output verifier. No acceptance promotion. PostgreSQL concurrency,
expiry, rollback and retry tests plus real-host latency measurement are
required; do not infer throughput improvement from the fence design.

### MVP-05C.1 — Scoped entity identities and grounded reference recall

Migration 0045 adds Memory-owned stable UUID identities and immutable
revision snapshots for explicit user-to-entity relations. PERSON, PLACE
and OBJECT identities have bounded relation types and exact normalized
name/alias keys. Names are not unique; never auto-merge or resolve by
recency. Explicit authenticated registration and amendment commands reuse
Memory receipts, evidence scope, selection checks, event stream/outbox,
optimistic versions and newer source chronology. Whole-source declarative
grammar validates the selected relation/alias/retraction. A source turn
may authorize only one entity revision. No automatic entity extraction
or inference from a name; explicit registration must choose a distinct
identity. Alias addition keeps the original relation evidence; correction
preserves stable ID and aliases. Retraction excludes recall while retaining
provenance. Initial names/aliases use a bounded Latin-script grammar;
privacy-sensitive/expired rows are excluded. No new external graph.

Exact private recall questions (e.g. "Do you remember who Alex is?",
"Who is Alex to me?", "Do you remember my colleague Alex?", "Do you
remember what Atlas is?") use canonical current owner data in the existing
response commit, including forced model-only delivery. Unknown and
multiple matches abstain/clarify; qualified questions and aliases can
disambiguate, same-name/same-relation collisions remain ambiguous.
Adjacent explicit pronoun questions ("How do I know them?", "Do you
remember what it is?") resolve only a unique KNOWN binding in the
immediately preceding delivered response in the same conversation. The
existing scoped grounding event retains entity ID, revision and actual
relation source; no extra conversational state owner. An intervening
message, undelivered or ambiguous answer cannot bind a pronoun. The
entity is re-read for every answer, not a cached name/relationship.

The current lookup shares the 05B historical revision lock, and entity
writes bump it atomically through the existing deferred trigger. No
entity row lock is acquired after the shared revision lock; no model
inference is under a lock. Registered exact entity source turns are
conservatively omitted from generic raw/episode recall so superseded or
ineligible relations cannot reappear through those paths. Recent dialogue
and all derived-summary lineage are not rewritten or comprehensively
fenced. No automatic command admission from ordinary user text, natural
short clarification follow-up, arbitrary pronouns, entity-to-entity
edges, name changes/alias removal, broad semantic/entity-conditioned
historical retrieval, or general identity resolution. These are follow-up
05C/05D/07B concerns. Explicit owner registration is an API contract,
not a new UI. No acceptance promotion or frozen benchmark changes.

### MVP-05C.2 — Conversational entity reports and delivered clarification

The bounded positive whole-source grammar recognizes name-like first-person
relation reports, explicit remember requests, "Another NAME is my RELATION"
as a distinct identity, and "Actually, NAME is my RELATION" as a correction.
Unmarked names require capitalized name-like tokens; quoted, qualified,
compound, hypothetical and unsupported relation inputs abstain. Dispatch and
typed owner evidence checks share the same whole-source relation parser.
Horizontal spaces/tabs are normalized for matching only (including names
and multiword relation labels); stored source text is unchanged and
internal line breaks remain unsupported. The report
is a user assertion, not verified identity. First reports can register when
the scoped name/alias is absent; matching eligible reports do not create
another identity or revision. Differing plain reports and ambiguous,
ineligible, unknown or incompatible corrections request identification
rather than changing any record. Explicit distinction creates a separate
identity; it never merges. No automatic alias discovery or semantic
matching is introduced.

Conversation dispatch uses a direct report route; forced model-only commit
also validates the persisted user text independently of model output.
Reuse typed owner commands, receipts, whole-source validation, scope,
selection, versions, immutable revisions and transactional event/outbox.
The owner mutation and response commit are atomic, after current-turn
fences; no learning on a failed or superseded response. Previously consumed
sources read the existing eligible identity without a duplicate revision.
All entity API writers take a short persona-scoped transaction advisory
lock after selection SHARE and before receipt/entity row/event locks;
natural report commits acquire it before conversation/turn row locks,
using non-key update locks for those rows to permit FK validation by
concurrent Memory writers. This serializes first-name absence checks,
including with explicit API registration. No lock spans inference. Existing identity revision triggers
still advance the historical read dependency; no schema migration.

Named recall that actually presents two distinct choices stores their IDs,
versions and relation labels in the existing trusted grounding event.
A bounded short phrase such as "My colleague" routes directly only if
that choice response was delivered before this input, in the same
conversation and selection generation, immediately adjacent, without an
intervening delivered tool follow-up. At commit re-read the original
named lookup and compare the full displayed candidate set. A unique
matching relation resolves; changed, wrong or still-ambiguous matches
request explicit identification. No identity mutation or goal/decision
authorization follows from a short reply. Generic pending-act creation
is skipped for grounded ambiguous entity responses to avoid competing
question state. Known adjacent pronouns reuse the stronger delivery gate.
Legacy events without choice snapshots do not create a short-reply route.

This is a deliberately bounded conversational admission/repair slice.
Name-like grammar is not proof of a real-world person or distinct identity.
A blocked name can produce a generic abstention without disclosing its
stored content; existence inference and full privacy/deletion policy
need further work. No persistent clarification across intervening topics,
automatic ambiguous mutation resolution, broad alias learning, general
pronoun inference, multi-relation identities, entity-to-entity edges or
entity-conditioned historical retrieval. No new scheduler/model pass or
acceptance promotion. Test concurrency and atomic rollback in PostgreSQL;
measure latency separately.

### MVP-05C.3 — Entity-bound historical evidence and directed relationships

Migration 0046 adds Memory-owned bindings to current Memory revisions or
existing interpreted episodes, and a bounded directed relationship per
ordered pair of stable entity IDs. Typed authenticated link/relationship
commands reuse selection, owner receipts, evidence scope, optimistic
versions, entity-write serialization, event/outbox and historical
revision triggers. Source quotes must include a uniquely resolving
relation-qualified label at a clause boundary for evidence binding;
relationship writes require the entire explicit pair assertion,
correction or retraction. Name-prefix, unqualified or ambiguous labels
do not prove a binding. The original authenticated source is retained;
a linked episode remains MODEL_INTERPRETED. No automatic extraction,
name merging, graph traversal, inverse/transitive inference, or duplicate
copy of Memory content. The initial relations are KNOWS, WORKS_WITH,
WORKS_AT and LOCATED_AT with compatible endpoint kinds. Correction
changes the accepted relation on the same ordered pair; retraction
retains immutable revisions. No automatic endpoint replacement.

The read API selects eligible current evidence by exact entity ID,
including a pinned Memory revision and exact source anchor or episode ID.
Owner corrections invalidate older bindings; explicit relinking is
required. Preserve current Memory/episode eligibility and evidence
class. Suppress repeat episode projections of the same source and
conservatively omit exact source turns linked to ineligible entities
from generic Memory/raw/episode recall. Relationship revision source
turns are governed by their owner, not generic recall. A relationship
is visible only while both endpoints are eligible. Original transcripts
and revision history are not erased.

Explicit history questions ("What do you remember about my friend
Alex?", "What do you remember about the entity Alex?") and supported
adjacent follow-up questions resolve one identity before selecting
bounded linked history. Adjacent references reuse the delivered known
entity gate; no binding from arbitrary model history prose. No lexical,
summary or verbatim history fallback, even within the current session,
on this explicit route. Preserve selected persona context, latest input
and optional communication/affect preferences; use one model pass.
Canonical fallback replaces model guesses on unknown, ambiguous,
missing or budget-excluded evidence. Successful history replies remain
model-generated from the supplied evidence, not generally fact-verified.
Record selected identity/version and actually supplied source-turn IDs
in the response context event. Reuse 05B historical revision/expiry
validation and its single bounded retry; entity expiry now participates
in the scope deadline. No locks span inference. The read API supports
absolute source report-time bounds; natural dated-entity history is
not part of this grammar.

This slice requires explicit owner commands to bind evidence and edges;
no automatic conversational edge/link admission, unlink operation,
entity-to-entity edge traversal, multiple parallel relations per pair,
as-of reconstruction, broad semantic identity resolution or full
derived lineage. Ineligible linked sources can conservatively hide
otherwise useful data from generic recall. No acceptance promotion.
Measure query/lock costs and independent conversational quality
separately.

## Tests

```bash
python -m pytest -q tests/test_model_comparison.py tests/test_mvp03c_natural_review.py tests/test_phase1_model_gateway.py
bash -n scripts/compare-persona-models
```

Real Ollama+PostgreSQL comparison remains a manual runtime acceptance test;
these unit tests use no model or Docker daemon.
