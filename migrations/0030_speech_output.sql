-- P5-03: per-clip, source-correlated speech output delivery.
-- The authoritative response remains conversation_responses; generated
-- PCM/WAV bytes stay transient and must not be committed to PostgreSQL.
ALTER TABLE conversation_responses
    ADD CONSTRAINT conversation_responses_speech_scope_uniq
    UNIQUE (response_id,user_id,instance_id);

CREATE TABLE speech_clips (
    clip_id uuid PRIMARY KEY,
    response_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    response_revision bigint NOT NULL CHECK (response_revision >= 1),
    channel_id text NOT NULL CHECK (char_length(channel_id) BETWEEN 1 AND 128),
    clip_index integer NOT NULL CHECK (clip_index >= 0 AND clip_index < 64),
    clip_count integer NOT NULL CHECK (clip_count BETWEEN 1 AND 64),
    playback_token uuid NOT NULL UNIQUE,
    format text NOT NULL CHECK (format = 'WAV_PCM16_MONO'),
    sample_rate_hz integer NOT NULL CHECK (sample_rate_hz IN (16000,22050,24000,48000)),
    duration_ms integer NOT NULL CHECK (duration_ms BETWEEN 1 AND 30000),
    payload_sha256 text NOT NULL CHECK (char_length(payload_sha256)=64),
    status text NOT NULL DEFAULT 'GENERATED'
        CHECK (status IN ('GENERATED','QUEUED','STARTED','PLAYED',
                         'INTERRUPTED','FAILED','TIMEOUT')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    queued_at timestamptz,
    started_at timestamptz,
    played_at timestamptz,
    UNIQUE (response_id,clip_index),
    FOREIGN KEY (response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id),
    CHECK (clip_index < clip_count),
    CHECK ((status='PLAYED')=(played_at IS NOT NULL))
);
CREATE INDEX speech_clips_expiry_idx ON speech_clips(updated_at,clip_id)
    WHERE status IN ('GENERATED','QUEUED','STARTED');
CREATE INDEX speech_clips_response_idx ON speech_clips(response_id,clip_index);
