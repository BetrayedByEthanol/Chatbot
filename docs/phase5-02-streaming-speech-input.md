# P5-02 — Streaming Speech Input (Architecture v2.5.1)

**P5-D01 remains intact:** exactly one Conversation Manager serves typed and
voice-derived user turns. This milestone adds transient PCM framing, bounded
voice activity segmentation, preview/final ASR requests and an optional local
ASR provider. No additional conversation engine, scheduler, state owner,
authority or database migration.

## Contract and processing

1. A **trusted authenticated host** binds TrustedActor and VoiceInputSource
   using server-side primary-user identity, channel and selected instance
   generation. Never derive these values from ASR text or voiceprint.
2. The host supplies mono little-endian PCM16 at 16 kHz, in ordered 20 ms
   / 640-byte Pcm16Frame messages. It is responsible for network authentication,
   source-device handling, resampling, backpressure and connection limits.
3. VoiceStreamSession.accept_frame performs only cheap RMS energy gating and
   bounded, contiguous sequence/clock validation. Activity begins after
   sustained voice energy; silence, stream EOF or maximum length ends it.
   Energy VAD is a provisional detector, **not** reliable speech classification
   or identity verification.
4. The session emits immutable DecodeRequest(PARTIAL|FINAL) snapshots. It does
   not invoke ASR, perform PostgreSQL reads, or touch Conversation or lifecycle.
   Transport can drop optional interim previews when overloaded. A bounded
   host executor runs recognize_request() outside PostgreSQL transactions,
   prioritizing finalization over interim previews.
5. VoiceStreamSession.apply_decode rejects stale/out-of-order partials, and
   exposes accepted partials as transient TranscriptUpdate(PARTIAL) only.
   It persists accepted FINAL transcripts by calling the existing
   multimodal_ingress.ingest_voice_transcript() -> conversation.ingest_text().
   The Phase 1–4 event/outbox, memory, context, cognitive, emotion, dialogue,
   tool/authorization and response paths are **not duplicated**.
6. Failed final commits stay FINAL_PENDING so the host can retry the same
   result using P5-01's scoped idempotency. Blank ASR finals become
   NO_TRANSCRIPT, with no fabricated user turn. Cancelled sessions discard
   late results; later persona-switch authority rejects stale final commits.

## Local ASR adapter

Install optional dependencies:

    pip install -e ".[asr]"

FasterWhisperRecognizer lazily loads an explicitly configured local
faster-whisper model, converts PCM16 to float32, and returns bounded
recognition text. It **re-decodes accumulated audio snapshots** for previews;
this is streaming *audio ingestion and interim hypotheses*, not native
token-streaming Whisper output. Native-streaming provider adapters can replace
this implementation behind SpeechRecognizer without altering Conversation.

For an actual provider smoke (with a consented 16-kHz mono PCM16 WAV):

    python scripts/phase5_asr_smoke.py path/to/short.wav --model tiny --language en

This smoke checks the local model and segmentation without an authenticated
production transport or committing a conversation turn. Real ASR accuracy,
model download/cache policy, GPU contention and end-to-end microphone latency
require target-hardware testing before acceptance; model-mock tests do not
constitute a live-provider pass.

## Limits and tuning

Defaults: 20 ms PCM16 frames, RMS 550, 60 ms voice activation, 600 ms endpoint
silence, 1.2 s earliest preview, roughly 1.2 s preview spacing, at most 30 s
recorded speech and 10 s idle. Maximum per-utterance buffered PCM is bounded.
These values are provisional and may need tuning for microphones, background
noise, speaker differences, language and interaction latency.

Raw audio and interim hypotheses remain transient. They are not recorded in
PostgreSQL or sent to the Cognitive Engine. Original captured_at timestamps
remain SOURCE_REPORTED, not independently verified current observation data.
The host must avoid TTS echo; acoustic loopback prevention, interruptions,
speaker/source arbitration and full duplex voice are P5-04 integration work.

**Operational boundary:** this code is an in-process, host-integrated
state machine/provider adapter, not an authenticated HTTP/WebSocket endpoint,
microphone driver, or standalone worker queue. Use the established resource
admission policy and bounded caller executor; do not start an unbounded decode
thread per 20-ms packet or store partial PCM in the operation journal.

## Acceptance

- Synthetic frames: segmentation, ordering, bounded memory, silence, EOF,
  cancellation, stale previews and repeated finals.
- Optional provider shim: decoding/shape, model reuse and failure semantics.
- Real PostgreSQL: exactly one FINAL turn, original event/provenance/outbox,
  text/voice context continuity, stale persona-switch rejection.
- Existing Phase 0–4 PostgreSQL and live Ollama acceptance must remain green.
- Live ASR smoke must be performed on selected hardware before treating local
  provider quality and latency as verified.

Do not merge without explicit user approval.
