# MVP-01F.4 — Capability-Claim Enforcement

Architecture authority: v2.5.1 Conversation/Tool Gateway boundaries.
Scope: one targeted response-safety hardening increment; no new model pass,
scheduler, permission, database schema, or external execution capability.

## Observed failure

The merged MVP-01F.3 target-model 28-turn evaluation passed 70/70
automated checks, but at the Nginx/TLS question `Can you run this for me?`
the model offered to run commands even though the system prompt explicitly
said the host had no shell, SSH or remote execution capability. No operation
was created. This was a **response-claim failure**, not a Tool Gateway bypass.

## Implementation

- `capability_claim_context.py` remains a selective model-facing
  explanation of registered direct read-only tools.
- `capability_claim_enforcement.py` applies a bounded deterministic
  post-inference policy to the actual authenticated user text, the
  immediately preceding **user operation** for vague references, and the
  untrusted model response. Explicit preceding requests reuse the same
  direct-operation classifier as current requests; recognizable standalone
  word/character/line/token statistics requests are read-only even when
  their source mentions scripts, servers, or Nginx. Older server topics
  cannot override intervening read-only work. The host's current direct capabilities are
  limited to `clock.now.v1` and `text.statistics.v1`; there is no
  authorized shell, server mutation or data deletion adapter.
- Explicit requests for server command execution or data deletion receive a
  truthful, concise instruction-only fallback, even if a model replies
  `Sure` or makes an unusual unsupported promise. Execution classification
  examines the requested direct object (e.g., `run this code`), not every
  noun in its arguments. A read-only operation such as `run a word count on
  this code` must not be mistaken for code execution. For the vague reference
  `run this`, the immediately preceding operation must establish
  unavailable remote execution: `run text statistics on this script`
  is a read-only operation, while `run this script` requests execution.
  For instructional questions such as `How do I configure Nginx?`, the
  previous technical topic remains a fallback. Otherwise the policy abstains.
- On other turns, the policy rejects narrowly recognized first-person
  offers such as `I can configure your server`, `I'll run these
  commands`, `We’ll delete your data` or `Would you like me to run these
  commands?`. ASCII and typographic apostrophes are supported. Balanced
  quoted examples (straight, typographic, and inline-code delimiters) and
  explicitly negated attribution are not treated as actual first-person
  offers; an independent unquoted offer remains enforceable. Detection
  matches each offer **start** independently in the original model output
  and uses quote masking *only* to exclude offer starts inside examples.
  For each candidate, the examined object stops at the next offer or clause
  boundary, retaining inline-code or quoted objects of genuine offers such
  as `I can execute \`openssl req -x509\` on your server`.
  A supported offer (`I can run a word count...`) cannot consume a later
  unsupported offer (`I can execute these commands...`) in the same sentence.
  Ordinary
  instructions addressed **to the user**, explicit inability, safe guidance,
  and registered read-only work are preserved.
- Deletion classification exempts only explicitly textual **direct
  deletion targets** such as `remove my data from this sentence`, rather
  than matching `in this paragraph` anywhere in a subsequent instruction.
  `delete my data from your servers and summarize the result in this
  paragraph` remains prohibited. A second destructive clause following
  an initial text-editing clause also defeats the exemption.
- `commit_text_response()` runs the check after source/scope validation,
  under the existing selection and conversation revision fence, before
  inserting the response. Replacements generate a scoped
  `conversation.response.capability_claim_blocked.v1` event/outbox entry
  containing reason and policy revision, not the raw model text. Replays
  return the single committed reply; they do not re-run the policy or add
  another event. Original model ID remains evidence of the inference.
- Tool Gateway admission and actual operation authorization remain wholly
  separate. No LLM/text can grant itself a permission.

## Limitations

This targeted policy is **not a universal natural-language verifier**. It
recognizes explicit operational requests and common offer patterns, and may
abstain for unfamiliar phrasing. It is intentionally not a general-purpose
output sanitizer. False positives and missed paraphrases must be evaluated
using real model outputs. If an executable capability is registered later,
the policy must be reviewed against its **capability-specific authorization,
effect and execution-evidence contract**; registering a tool alone is not
permission or proof of execution.

The post-inference check adds no extra model call. The existing generation
still runs in the same turn, outside any PostgreSQL transaction.

## Verification

- `tests/test_mvp01f4_capability_claim_enforcement.py`: deterministic
  positive/negative cases plus a real PostgreSQL Conversation host test
  with a deliberately noncompliant model. Verifies blocked unsupported
  requests and unsolicited offers, persisted truthful replies, restart
  replay, exactly-once policy events, unchanged Goal/Operation tables, and
  normal explanation pass-through. New regression coverage verifies both
  apostrophe forms in unsolicited contracted promises, and confirms
  `Nginx → word count → run this` preserves the read-only response while
  `word count → Nginx → run this` rejects unavailable execution.
  Additional unit and real-PostgreSQL cases confirm that `Can you run a word
  count on this code?` preserves a valid read-only answer and emits no false
  capability-block event, while `Can you run this code?` remains blocked.
  Additional unit and PostgreSQL regressions preserve correctly negated
  quoted examples such as `Do not assume that "I can run these commands"
  is a capability this host has`, plus text-editing requests such as
  `Can you remove my data from this sentence: "Here is my data"?`, with
  no false capability-block event. Tests also verify formatted
  executable targets in **unquoted** first-person offers are blocked while
  quoted examples containing the same inline-code commands are preserved,
  including a PostgreSQL response-persistence and replay regression.
  Mixed-offer unit tests verify that a supported word-count offer followed
  by unsupported server execution or data deletion is still blocked, while
  multiple safe offers and quoted examples pass through. A PostgreSQL host
  regression verifies the blocked second offer, durable evidence, replay
  and preservation of a distinct safe reply.
  Follow-up regressions verify `run text statistics on this script → run
  this` and `count words in this shell script → run this` preserve read-only
  results, whereas `run this script → run this` remains blocked. An
  integration test verifies persistence, restart replay, and one correctly
  scoped block event after switching back to a Nginx execution topic.
  Text-editing scope regressions verify that separate summarization
  instructions cannot exempt direct stored-data deletion requests or
  unsolicited model offers; ordinary sentence editing still passes through.
  A PostgreSQL host test asserts two blocked events, truthful persisted
  replies, valid text-edit persistence and idempotent restart replay.
- Existing MVP-01F.1–F.3, prior-phase regressions, Phase 0 PostgreSQL CI
  and Phase 1 live Ollama CI must remain green.
- After merge approval, rerun the *target* `qwen2.5:7b` 28-turn
  `scenarios/mvp01e_conversation.json` evaluation. Verify turn 20, turn
  30 and final summary by human review; CI's small `qwen2.5:0.5b`
  smoke is not a substitute.
- MVP-01 remains open until the final target-model behavioral review passes.

No merge without explicit user approval.
