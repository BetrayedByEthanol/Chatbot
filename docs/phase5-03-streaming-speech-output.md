# P5-03 — Streaming Speech Output (v2.5.1)

Architecture: Phase 5, Conversation/Communication separation, R08 per-clip
playback acknowledgment, R09 output audio validation, R10 provider descriptors
and R12 authorized channel fallback. **P5-D01 remains confirmed:** one
Conversation Manager, one authoritative response state, regardless of output
modality.

## Scope and flow

1. The existing Conversation Manager produces a **completed, revision-fenced**
   text response. P5-03 never synthesizes raw speculative LLM tokens.
2. An authenticated host binds TrustedActor and SpeechOutputTarget to a
   user-authorized playback channel. A generated model instruction never
   grants playback or fallback authority.
3. stream_response_speech checks active selected Foreground persona, same
   response generation and latest input, then segments the committed response
   into <=64 bounded, stable text clips.
4. The selected TTS adapter produces one PCM16 mono WAV per clip outside a
   database transaction. WAV RIFF length, sample rate, channels, sample width,
   sample count and duration are checked **before** any clip is queued.
5. prepare_speech_clip durably records clip ID, index/total, response/context
   revision, channel, WAV digest, duration, playback token, and a GENERATED
   event/outbox. It does **not** store raw audio in PostgreSQL.
6. Each yielded GeneratedSpeechClip has a transient WAV payload for the
   authenticated output host to enqueue. The host can start playing one clip
   before synthesis of the entire response completes. **This is clip-level
   streaming**, not native token or sample streaming. Piper may internally
   synthesize each clip non-streamingly.
7. Playback callbacks supply scoped response_id, clip_id, playback_token and
   channel_id. Valid transitions:
   GENERATED -> QUEUED -> STARTED -> PLAYED; QUEUED/STARTED may become
   INTERRUPTED or FAILED; any unconfirmed in-flight state may TIMEOUT.
8. Only when **all expected clips are PLAYED**, and the same persona generation
   and latest response are still authoritative, does Communication atomically
   mark the existing conversation_responses row DELIVERED and emit the existing
   conversation.response.delivered.v1 event. This preserves the existing
   context builder's conservative definition of actually-heard assistant
   speech. Earlier played clips or interruptions do not imply the entire
   reply was heard.
9. A callback for an old clip cannot complete a new clip. A late PLAYED after
   a persona switch may record its historical physical disposition, but may
   not reactivate the persona, mark old reply DELIVERED or mutate attention.
   Attempts to start/queue stale speech are rejected.
10. A trusted host uses the existing operation/resource scheduler to invoke
    timeout_stale_speech_clips. It may record TIMEOUT after missing playback
    evidence, without asserting that audio was heard. There is no new worker.

## Provider

An optional local PiperCliSynthesizer invokes the operator-provisioned Piper
CLI with an explicit local model path, model sample rate and execution timeout.
There is **no** automatic download, cloud fallback or shell execution.
\`--output-raw\` PCM16 is wrapped in WAV and validated. Operators must verify
that configured sample rate matches the model's native rate.

For an offline smoke (no DB and no client playback):

    python scripts/phase5_tts_smoke.py --model /local/voice.onnx \
        --sample-rate 22050 --output /tmp/short.wav \
        --text "Hello there."

Actual Piper installation, model voice, latency, CPU/GPU budget and audible
quality must be tested on target hardware. The repository integration tests
use a controlled TTS stub, not a live voice provider. The generated WAV file
is a local operator-selected smoke artifact, not conversation persistence.

## Persistence and security invariants

- Migration 0030 adds speech_clips with strict scope to the existing
  conversation_responses row and immutable (response_id,clip_index) identity.
- No PCM/audio packet or transient preview in PostgreSQL, events or cognition.
- Playback token is **correlation**, not authentication. A trusted channel
  handler must authenticate user/session and bind the channel independently.
- Competing ACKs serialize under row locks; duplicate callbacks return replay
  results, and terminal states cannot be overwritten.
- Clip preparation and initial QUEUED/STARTED states require current selection
  and conversation response. Late terminal feedback may only update historical
  clip disposition. Interrupted/failed/timed-out speech is never marked heard.
- Re-synthesis after process restart can produce a different WAV digest;
  such a collision fails closed rather than silently replacing a previously
  recorded clip. A production media cache/recovery policy is an explicit
  follow-up for the real client transport.
- Full duplex input/output, echo rejection, speech barge-in and active
  client-driven interruption arbitration are P5-04. This slice supports
  INTERRUPTED acknowledgment **semantics** only.
- A provider failure or malformed WAV before a validated clip is created
  does not create a playable clip; the host must report generation failure
  and may use only an explicitly approved fallback policy. Future durable
  generation-failure tracking can be added without faking playback.

## Acceptance

- Full response becomes DELIVERED **only** after all expected clips
  acknowledge PLAYED; partial/missing/interrupt evidence remains not-heard.
- Unknown clip/response/channel/token/user callback cannot change state.
- Late callbacks after persona switch cannot regain reply authority.
- Invalid/malformed PCM/WAV never enters the playback queue.
- Concurrent/replayed ACKs emit only one event for each real transition.
- Explicit timeout is durable and never marks speech played.
- Existing Phase 0–4 conversation and notification semantics remain intact.
- The optional live provider smoke must be tested separately on hardware.

Do not merge without explicit user approval.
