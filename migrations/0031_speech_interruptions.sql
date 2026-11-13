-- P5-04: durable per-response speech stop barrier, distinct from dialogue
-- and unrelated asynchronous task cancellation. Audio remains transient.
CREATE TABLE speech_output_interruptions (
    interruption_id uuid PRIMARY KEY,
    response_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    output_channel_id text NOT NULL CHECK (char_length(output_channel_id) BETWEEN 1 AND 128),
    input_channel_id text NOT NULL CHECK (char_length(input_channel_id) BETWEEN 1 AND 128),
    utterance_id uuid,
    reason text NOT NULL CHECK (reason IN ('USER_SPEECH','CLIENT_STOP')),
    interrupted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id),
    CHECK ((reason='USER_SPEECH')=(utterance_id IS NOT NULL))
);
CREATE INDEX speech_output_interruptions_scope_idx
    ON speech_output_interruptions(user_id,instance_id,interrupted_at);
