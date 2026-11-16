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
  post-inference policy to the actual authenticated user text, only the
  immediately preceding user topic for vague references, and the
  untrusted model response. Older server topics cannot override intervening
  read-only work. The host's current direct capabilities are
  limited to `clock.now.v1` and `text.statistics.v1`; there is no
  authorized shell, server mutation or data deletion adapter.
- Explicit requests for server command execution or data deletion receive a
  truthful, concise instruction-only fallback, even if a model replies
  `Sure` or makes an unusual unsupported promise. For the vague reference
  `run this`, the immediately preceding user topic must establish the
  server/command context. Otherwise the policy abstains.
- On other turns, the policy rejects narrowly recognized first-person
  offers such as `I can configure your server`, `I'll run these
  commands`, `We’ll delete your data` or `Would you like me to run these
  commands?`. ASCII and typographic apostrophes are supported. Ordinary
  instructions addressed **to the user**,
  explicit inability, safe guidance, and registered read-only work are
  preserved.
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
- Existing MVP-01F.1–F.3, prior-phase regressions, Phase 0 PostgreSQL CI
  and Phase 1 live Ollama CI must remain green.
- After merge approval, rerun the *target* `qwen2.5:7b` 28-turn
  `scenarios/mvp01e_conversation.json` evaluation. Verify turn 20, turn
  30 and final summary by human review; CI's small `qwen2.5:0.5b`
  smoke is not a substitute.
- MVP-01 remains open until the final target-model behavioral review passes.

No merge without explicit user approval.
