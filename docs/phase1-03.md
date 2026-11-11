# P1-03 — Dialogue State & Observability

**Scope:** The final contract-first vertical slice of Phase 1 Responsive Core.
It is a minimal conversation-state implementation, not Phase 2 memory/cognition
or Phase 3 task/procedure execution. Authority: v2.5 Sections 12, 14, 18,
23.1–23.2 and the cognitive C01, C03, C07, C13 test scenarios.

## Domain-owned persistent dialogue progress

Migration 0007 adds conversation-scoped dialogue threads, explicit turn links,
pending dialogue acts and evidence-linked topic/entity referents. The existing
\`conversations.revision\` is the optimistic lock/watermark for state and
prompt context. The current focus belongs to the conversation row.

- \`DialogueThread.attention_status\`: FOCUSED, RECENT or DORMANT
- \`DialogueThread.resolution_status\`: OPEN, RESOLVED or ABANDONED,
  independent of focus and of task verification
- \`dialogue_pending_acts\`: QUESTION, DECISION or ACKNOWLEDGMENT with
  a DELIVERED source response and explicit status
- \`dialogue_referents\`: typed TOPIC or ENTITY label and source turn; labels
  are not elevated to instructions or verified factual assertions
- \`dialogue_turn_links\`: explicit association of a user input with one
  thread, used as durable provenance for returning to a topic

Contracts: OpenThreadCommand, FocusThreadCommand, SetThreadProgressCommand,
OfferDialogueActCommand, ResolveDialogueActCommand and
RegisterReferentCommand. Each command requires a trusted actor,
matching selected Foreground persona generation, source input equal to
the latest recorded turn, and an expected conversation revision.
Each successful mutation commits a receipt, event, outbox and state
together; retries replay the same receipt, while conflicting intent
fails with IdempotencyConflict or VersionConflict.

No new LLM/classifier is required for state updates. The orchestrator or
trusted ingress can supply typed state proposals based on an ordinary
conversation turn. The deterministic preflight resolves a literal
\`ok\`, \`okay\`, \`got it\` or \`understood\` only if the focused thread
has exactly one pending ACKNOWLEDGMENT. It does **not** resolve
DECISION or QUESTION acts, grant permission to perform actions, or
mark a procedure as executed. Ambiguous acknowledgments are left pending.

\`load_active_dialogue\` reconstructs persisted state without activating
an inactive persona. Prompt assembly includes a small capped, explicitly
untrusted metadata representation of the focused topic, next action,
pending acts and referents. Recent verbatim conversation, role boundaries,
context budgets and selection fences remain unchanged.

## Delayed-tool relevance

P1-02 already retained tool results when newer conversation displaced
their turn. P1-03 permits those results to become READY again when:

1. The original and latest user input have been explicitly linked to the
   same thread using typed conversation commands.
2. That thread is currently focused and conversationally OPEN.
3. The newer input has received a DELIVERED assistant response, to avoid
   interrupting the active reply.
4. The original deferred notice was delivered, and the operation completed
   with its original lease/result authority.
5. The selected Foreground persona and generation remain unchanged.

These checks run in reconciliation, preparation and delivery acknowledgment
under the existing conversation and persona-selection lock discipline.
A text turn arriving while the old thread remains focused is insufficient
without its own explicit turn-thread evidence link. An inactive persona
is never silently reactivated; stale/suppressed results stay suppressed.

## Read-only observability

\`get_turn_trace\` and \`get_conversation_metrics\` derive measurements from
the authoritative user-turn, response, operation, outbox-linked event
journal and follow-up records. Data reads are authenticated and
user/conversation scoped. The API does not return message contents.

- Per-turn: input timestamp, generated/delivered response timestamps,
  time-to-output-ready, time-to-delivery, correlated operation/follow-up
  status and event IDs.
- Per-conversation: bounded latest-N sample (1–500), generated/delivered
  counts, pending turns, mean and nearest-rank P95 latency, active tool
  backlog/count/age and deferred follow-up count.

**Important measurement limit:** Time-to-output-ready for the current
non-streaming model is not time-to-first-token, speech playback progress,
GPU utilization, token spend, quality of retrieval or evidence of
production SLA compliance. No invented values are returned for unsupported
signals. Metrics are read-only; they add no extra model invocation or
per-turn synchronous telemetry write.

## Execution, tests and follow-on

\`tests/test_phase1_dialogue.py\` covers focus switches, resolved-but-focused
threads, restart restoration, trivial acknowledgment routing, typed pending
questions/decisions, source-linked referents, unauthorized/stale commands,
prompt budgets, turn metrics and delayed tool relevance after an explicit
topic resume. Existing Phase 0, P1-01 and P1-02 tests run unchanged.

A full Phase 1 acceptance demo still needs an actual interactive host
(transport and Ollama/provider config), a supervised worker process and
measured latency under its real resource constraints. Those deployment and
production integration steps are not established merely by passing the
contract/integration test suite.

Extended historical episodic retrieval, nuanced ambiguity classification,
language-model-driven state proposals, cognitive attention, emotional
adaptation, and durable ProcedureRun task-step verification remain Phase
2/3 responsibilities. Avoid modifying approved stable procedures merely
because their associated dialogue progressed.
