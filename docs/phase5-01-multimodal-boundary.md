# P5-01 — Shared Conversation Manager multimodal ingress boundary

Architecture: Persistent Persona Agent Architecture v2.5.1, Phase 5, P5-D01
(one Conversation Manager across modalities); behavioral refinements R07/R10.
This slice is a contract boundary and deterministic transcript adapter,
NOT an ASR provider, streaming transport, new scheduler, or voice-output path.

## Ownership and data flow

Authenticated transport binds TrustedActor and VoiceInputSource from server-side
identity, selected instance generation and channel identity. These values MUST
NOT be taken from a transcript or model output.

ASR hypothesis -> TranscriptUpdate(PARTIAL) -> ephemeral preview only

ASR final -> TranscriptUpdate(FINAL) -> multimodal_ingress.ingest_voice_transcript
-> existing conversation.ingest_text -> existing conversation_turns,
conversation.user_text.received.v1 event and outbox -> existing context,
memory, cognition, emotion, dialogue state and Model Gateway.

All response generation, contextual assembly, response revision and delivery
remain owned by the existing Conversation Manager and Communication system.

## Security and persistence

- PARTIAL performs no database writes, no model call and no selection change.
  Preview buffering, rate limits, packet ordering, authentication and stream
  cancellation belong to the future authenticated client/ASR transport.
- FINAL requires authenticated TrustedActor, a trusted host channel binding,
  final nonblank bounded text and a timezone-aware capture timestamp.
- The existing conversation command enforces tenant scope, selected Foreground
  persona generation and idempotent finalization, including worker races.
  Exact replay remains possible after later persona switches, but it cannot
  grant new response-delivery authority.
- The stable finalization key is SHA-256 of conversation, channel and utterance.
  Different final text for the same key is rejected using existing receipts.
  The original observation metadata is retained on replay.
- Existing event type conversation.user_text.received.v1 is intentionally
  preserved so all Phase 2 consumers process committed voice-derived text
  using the same semantics as typed user input.
- Voice event payload adds input_modality, utterance_id, channel_id,
  captured_at and capture_time_basis=SOURCE_REPORTED. Its received/recorded
  time remains the trusted event journal timestamp. Client/provider
  captured_at is unverified observation metadata; it must not alone justify
  a present-tense perception claim.
- Existing text command fingerprint and event payload are unchanged;
  previously committed text idempotency receipts remain replay-compatible.
  No SQL migration or additional authoritative state owner is required.

## Limitations

This boundary does not process audio, identify speakers, authenticate client
connections, deduplicate partial packet revisions, persist raw audio, recognize
speech, or mark audio played. Unknown/third-party speaker content is not
authorized by these contracts. Actual ASR input and interruption management
will be added in P5-02 and P5-04. A host must never construct TrustedActor
from speaker recognition or arbitrary platform names.

## Acceptance

- Typed final text and voice-derived final text produce turns in exactly the
  same conversation, context, memory and model gateway.
- Partial updates are nonauthoritative and cannot trigger a task/tool.
- Final duplicate/race produces one turn, event, outbox and command receipt.
- Conflicting final content for an utterance is rejected.
- Wrong tenant or inactive persona cannot create a new final turn.
- Capture provenance is durable and distinct from trusted receipt time.
- Existing Phase 0–4 acceptance remains unchanged.

No merge without explicit user approval.
