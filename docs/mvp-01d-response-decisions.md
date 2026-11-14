# MVP-01D — Response Decisions & Natural Tool Routing

Authority: Persona Architecture v2.5.1 §18.1, and the established MVP-01
roadmap (deterministic preflight → selective interpretation → contextual
resolution → response decision). This is a **bounded first vertical slice**,
not general model-directed tool use or final MVP-01 acceptance.

## Scope and processing contract

- Trusted local text transport now defaults to `mode="auto"`; existing
  explicit operator modes `model`, `time` and `analyze` still work. The
  Behavioral Acceptance Harness can continue exercising explicit model mode.
- `LocalAcceptanceSession.message` first persists authenticated user input
  through existing `conversation.ingest_text`, selection checks, intent
  signals and the durable input event. For auto mode it delegates to
  `response_orchestrator.respond_to_user_text`: the **Conversation
  Orchestrator**, not the transport adapter, owns the decision. It examines
  the **stored input** scoped by authenticated user and persona; it does not
  parse the assistant response, web content, or a user-provided mode command.
- `response_decisions.decide_response` is deterministic and abstaining.
  It selects only two *already registered, READ_ONLY* capabilities:
  `clock.now.v1` and `text.statistics.v1`. The full-match grammar
  rejects hypothetical, quoted, ambiguous, future or unsupported requests.
  All other text uses the existing primary model response path and its
  contextual intent/dialogue/memory prompt.
- The clock route accepts precise user-stated UTC/GMT, a bounded city
  mapping, or a valid IANA timezone; unspecified time uses the trusted
  local adapter's configured timezone (UTC by default), not guessed user
  geolocation. Unrecognized place names **abstain** rather than guessing.
- Analysis requires a concrete delimited payload, e.g.
  `Count words in: alpha beta beta` or `Analyze this text: alpha beta beta`.
  The existing asynchronous worker analyzes just that source-grounded
  payload rather than including the command prefix. Legacy explicit
  `mode="analyze"` with plain text continues to analyze the entire input.
- The response is committed through existing tool or conversation
  contracts, not a new scheduler: scoped turn/response persistence,
  selection and revision fences, read-only capability validation,
  durable operation/worker lease/cancellation, short defer notice,
  follow-up eligibility, output ACK, crash replay and event/outbox.
  No model inference, external tools or database locks are introduced for
  the two high-precision routes. No new database schema or tool permission.
- The inferred route is reproducible from the immutable authenticated turn.
  Durable final response `model_id` and, for slow tools, tool-call/operation
  state provide the authoritative execution record. This slice does not
  persist a separate speculative decision object.

## Boundary examples

| Input | Route |
| --- | --- |
| `What time is it in Berlin?` | `clock.now.v1` with `Europe/Berlin` |
| `What time is it?` | clock with trusted adapter timezone |
| `Count words in: red blue blue` | `text.statistics.v1` on `red blue blue` |
| `What time is it tomorrow?` | conversational model (no future-time inference) |
| `What time is it on Mars?` | conversational model (unknown timezone) |
| `I want to eat healthier.` | conversational model, existing tentative objective signal |
| `Analyze this text for me` | conversational model (no payload) |
| `What if I wanted to use the clock?` | conversational model (hypothetical) |

This does not interpret or authorize goal approvals, procedural execution,
unrestricted model-proposed tool calls or arbitrary timezone lookup.
General model-directed response plans and natural confirmation/clarification
quality remain separate MVP-01 follow-ups.

## Verification

Run in an existing disposable PostgreSQL CI database after migrations:

```bash
python -m pytest -q tests/test_mvp01d_response_decisions.py
```

Coverage: deterministic positive/negative routing; authenticated persisted
turn; time capability without a worker; replay after host restart; deferred
text analysis and real durable worker/follow-up; analysis payload not command
prefix; explicit operator overrides; one-model fallback; untrusted
model-output command rejection; and no goal side effects.

The complete PostgreSQL contract suite and real-Ollama acceptance workflow
must pass before merge. Tests with mock model outputs do not establish
semantic dialogue quality or a calibrated classifier.
