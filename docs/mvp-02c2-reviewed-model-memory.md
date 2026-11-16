# MVP-02C.2 — Reviewed model-assisted memory acquisition

Authority: Architecture v2.5.1, P2-01 Memory and P2-04 extraction/ownership.
Baseline: MVP-02C.1 merged on main (`b13dbc98`).

## Increment, authority and limits

The optional model-assisted worker proposes **at most one** source-linked,
first-person, current **activity-enjoyment preference** from an authenticated
and already responded conversation turn, e.g. `I've gotten into gardening
lately.` → proposed `PREFERENCE user.enjoys_gardening=yes`. It uses one
bounded background model call only for eligible messages; never adds
foreground inference. It **does not** automatically promote a model inference
into a verified fact or an authoritative Memory record.

Model proposals are stored separately in `memory_model_candidates` with
status `PENDING_REVIEW`, the user, persona, conversation, immutable source
turn and event, exact source quote, model ID and confidence. Model confidence
is not truth confidence. The durable outbox/leased delivery consumer uses
P0 delivery/fencing, including attempt handling and inactive-persona
deferral. Before inference a read-only source lookup checks the authenticated
event, response status, selected persona and its current generation;
after inference the P0 DB-only handler revalidates the lease, immutable source
and selection generation before writing the candidate. No DB transaction
is open during LLM network inference.

The proposal schema and strict independent gate allow only
`user.enjoys_<non-sensitive allowlisted activity> = yes|no`, an exact full
user-input evidence quote, bounded input text, and a safe topic/value.
Questions, speculation and sensitive-topic markers are rejected, as are
model-invented values, unsupported fields, source mismatches and
unapproved tool/goals. This remains a **limited heuristic privacy policy**,
not a claim that arbitrary free text is safe to store. Broader source
classification, explicit consent and retention controls must precede any
general model-assisted memory extraction.

### User ownership

`list_model_memory_candidates()` is authenticated and scoped to the
selected user/persona; candidates for inactive personas remain inspectable.

`approve_model_memory_candidate()` is an **explicit TrustedActor** action.
It locks the candidate, rechecks the source, topic and value, and calls the
Memory-owned `_admit_memory_in_transaction()`. Candidate status, Memory record,
version, command receipt, revision event and outbox commit or roll back
**atomically**. An already occupied conflicting slot cannot be replaced by
model approval: the established P2-01 Memory correction flow remains
separately user-owned. Approving an existing identical value is idempotent.
`reject_model_memory_candidate()` marks a candidate rejected without
touching Memory. None of these actions is authorized for the model.

### Conflict-aware retrieval

A current `memory_records` value remains **authoritative in PostgreSQL**
until user-approved correction, but the scoped memory search/query omits the
slot when a **newer source-linked unresolved P2-04 conflict** targets its
current version. This prevents an assistant from confidently asserting an
old preference while awaiting correction, without silently rewriting history.
Non-conflicting memories remain searchable. The user may inspect pending
conflicts and explicitly approve a correction using P2-04's existing API.
Pending *model* candidates do not suppress authoritative records.

## Acceptance

PostgreSQL regressions validate worker-claim lifecycle, no model inference
inside transactions, provenance, candidate-review-only persistence,
cross-user isolation, invalid output rejection, atomic rollback, idempotent
approval, new-session source-linked recall and suppression of unresolved
newer contradictory memories.

```sh
python -m pytest -q tests/test_mvp02c2_reviewed_model_memory.py
```

The optional worker requires migrations through `0037` and an authorized
local Ollama endpoint. Register the new consumer **before** dispatching
source events, or use existing explicit P0 subscription backfill. Keep the
existing deterministic memory worker running separately:

```sh
DATABASE_URL=postgresql://... python -m persona_agent.memory_model_extraction \
  --model qwen2.5:7b
```

Target-model acceptance runs against a fresh disposable PostgreSQL instance,
uses the actual model as both conversational generator and candidate generator,
and explicitly scripts an **authenticated user approval**, not an assistant
tool action:

```sh
bash scripts/run_persona_e2e.sh \
  --model-memory-worker \
  --scenario scenarios/mvp02c2_reviewed_model_memory.json \
  --report artifacts/mvp02c2-review.zip
```

The ZIP contains outbound Ollama request/response evidence, source-linked
candidate snapshots before/after approval, and historical recall prompts.
`expect_model_memory_proposed` is a deterministic contract for the real
target model run; if the model abstains, preserve the ZIP and inspect its
response. Conversational correctness is still human-reviewed.

### Deferred

No inferred general personal facts, sensitive memories, episodic experience
promotion, model-approved corrections, automatic Goal creation or general
semantic graph consolidation. These require a richer proposal contract,
explicit privacy/temporal scope and owner-governed evaluation. Avoid adding
an autonomous memory-write path or running LLM calls in P0 delivery handlers.
