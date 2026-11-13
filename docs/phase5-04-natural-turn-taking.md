# P5-04 — Natural Turn-Taking and Barge-In (v2.5.1)

**One Conversation Manager.** This milestone adds an authenticated,
in-memory full-duplex coordination boundary and an atomic PostgreSQL
speech-stop barrier. No new persona lifecycle manager or worker service.

## Process: media-host responsibility and agent responsibility

1. An authenticated host binds one VoiceStreamSession (P5-02), selected
   conversation/persona/generation, committed response ID (P5-03) and
   authorized physical speaker/microphone channels. It sets EchoPolicy.
2. **Default** BLOCK_MIC_DURING_OUTPUT suppresses all mic frames while
   playback is active. A bounded 10-frame (200-ms) tail guard suppresses
   residual playback after a normal stop. No audio is decoded/committed
   from these suppressed frames. This is safe but cannot hear barge-in.
3. **VERIFIED_AEC** requires the trusted host's *actual* acoustic echo
   cancellation and verification. Given echo-reduced microphone PCM, the
   existing VAD requires 3 voiced frames (default ~60ms) before it signals
   user speech. The coordinator does not assume that RMS energy distinguishes
   user voice from loudspeaker echo. Hardware/room-specific evaluation is
   required before permitting full duplex. The host must not select
   VERIFIED_AEC merely because a client reports that it is capable.
4. **Before every physical playback start or resume**, including an
   inter-clip gap, the host must call set_playback_active(True) and check its
   boolean result. When speech is already RECORDING, FINAL_PENDING or
   COMMITTED, the controller returns True, refuses to activate playback and
   sets the response cancellation signal. The host must flush pending audio,
   persist interrupt_command(), and never start/resume that response.
   Playback already underway instead reports an early interruption via
   TurnFrameResult.interrupt_requested at accepted VAD onset. In either case
   the host **immediately** stops the physical speaker and flushes queued
   audio, cancels the response-local TTS generator Event, and commits
   interrupt_speech_response() with the selected
   response/persona/channel/utterance binding. **Never await DB work before
   stopping physical playback.** Retain input PCM and continue VAD/ASR.
5. The stop transaction locks selection, conversation, response and existing
   clips. It writes exactly one speech_output_interruptions barrier and
   transitions generated/queued/started clips to INTERRUPTED with per-clip
   events and a response-level event in the existing outbox. Already PLAYED
   clips retain evidence of partial hearing. A fully DELIVERED response
   cannot be undone. Late clip acknowledgments cannot mark INTERRUPTED
   audio PLAYED or mark the entire response delivered.
6. After the host records the stop barrier, P5-03 prepare_speech_clip rejects
   late TTS completions (even if the provider call could not be cancelled).
   The optional should_cancel callback in stream_response_speech avoids
   unnecessary inference between clips and discards completed audio when
   cancellation occurred mid-clip. TTS never holds a DB transaction.
7. Once the voice session reaches FINAL, its preexisting
   VoiceStreamSession.apply_decode() -> multimodal_ingress ->
   conversation.ingest_text() commits **one** user turn, with the original
   user-authentication and persona-selection checks. Provisional ASR
   hypotheses remain ephemeral.
8. Speech cancellation is **response-local**. It does not cancel,
   supersede or mutate unrelated background operations, user tasks,
   procedure runs or goals. The existing Conversation Manager owns the
   subsequent dialogue.
9. The host must authenticate callbacks and bind output channels; IDs and
   correlation tokens are never authentication. TurnFrameResult is a
   *suggestion from bounded perception*, not an instruction from the model.
10. Physical output disconnects and explicit CLIENT_STOP can use
    SpeechInterruptCommand without a microphone utterance. The host must
    not migrate playback to another output channel without a separately
    authorized fallback policy.

## Echo and truthfulness limitations

- No bundled real AEC, beamformer or speaker diarization model is claimed.
  The safe default blocks microphone echo but temporarily suppresses genuine
  interruptions. VERIFIED_AEC may detect real barge-in; it requires a real
  host-supplied echo-cancelled microphone signal. The energy threshold still
  needs acoustic calibration and is not speaker authentication.
- No network transport, browser audio player or microphone driver is
  implemented in P5-04. The host must enforce bounded work, per-channel
  audio ownership, and stop/flush local playback before persisting a stop.
- An ASR transcript identical to previous assistant speech is **not**
  automatically rejected: users can legitimately repeat text. Self-echo is
  prevented at the audio input boundary rather than inventing semantic
  heuristics that might suppress valid speech.
- Interrupted responses remain GENERATED in Conversation so the existing
  context builder never misrepresents the whole response as heard. The
  durable clip records preserve what was actually confirmed PLAYED; an
  interruption leaves unacknowledged hearing uncertain. Do not infer
  procedure/task completion from spoken or generated text.
- An identical authenticated interruption command replays its committed
  receipt after a newer user transcript or persona selection change.
  Authentication, immutable source scope and interruption origin still must
  match; freshness checks apply only when creating a new stop.
- A live ASR/TTS full-duplex smoke and actual AEC hardware integration are
  required before declaring real-time conversational voice acceptance.

## Contracts and migration

- \`migrations/0031_speech_interruptions.sql\`:
  per-response one-time stop fence scoped to user, persona and generation.
  Stop commands additionally match the source conversation ID.
- \`turn_taking.py\`: no database, no provider calls, no audio persistence.
  Emits one early interruption signal after accepted speech onset.
- \`speech_interruptions.py\`: durable stop, per-clip interruption and
  transactional events/outbox; idempotent retried host signals.
- \`speech_output_state.py\`: durable barrier before clip creation, optional
  response-scoped inference cancellation between/in response clips.
- Existing lifecycle selection, ASR FINAL ingress, playback ACK state and
  Conversation delivery semantics are reused without redesign.

## Tests

- Echoing speech during BLOCK_MIC_DURING_OUTPUT produces no ASR request,
  interruption or conversation turn; suppressed PCM keeps valid external
  ordering and prevents sequence gaps on later mic frames.
- VERIFIED_AEC onset requests stop before a final ASR transcript; pending
  utterance remains eligible for normal FINAL ingress after physical stop.
- PostgreSQL verifies progress-preserving interruption, one stop barrier,
  immutable clip identity, replay and concurrent stop idempotency, invalid
  channel/persona/user, late TTS after stop, completed playback cannot be
  undone and no false delivered history.
- Existing Phase 0–5 tests and live Ollama acceptance remain mandatory.

**Do not merge without the user's explicit review and approval.**
