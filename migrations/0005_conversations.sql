-- Phase 1 slice 01: one scoped, revisioned text conversation per conversation_id.
-- This slice persists recent turns and response delivery; full topic/thread,
-- procedural state, memory compression, and tool continuation follow later.
CREATE TABLE conversations (
    conversation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    next_sequence bigint NOT NULL DEFAULT 1 CHECK (next_sequence >= 1),
    last_input_turn_id uuid,
    summary_watermark bigint NOT NULL DEFAULT 0 CHECK (summary_watermark >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id, user_id, instance_id),
    FOREIGN KEY (user_id, instance_id) REFERENCES persona_instances(user_id, id),
    CHECK (summary_watermark < next_sequence)
);

CREATE TABLE conversation_turns (
    turn_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 1),
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    source_id text NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 128),
    input_text text NOT NULL CHECK (char_length(input_text) BETWEEN 1 AND 12000),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RESPONDED','SUPERSEDED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id, sequence),
    UNIQUE (conversation_id, turn_id),
    FOREIGN KEY (conversation_id, user_id, instance_id)
        REFERENCES conversations(conversation_id, user_id, instance_id)
);
CREATE INDEX conversation_turns_recent_idx
    ON conversation_turns(conversation_id, sequence DESC);

ALTER TABLE conversations
    ADD CONSTRAINT conversations_last_input_fk
    FOREIGN KEY (conversation_id, last_input_turn_id)
    REFERENCES conversation_turns(conversation_id,turn_id);

CREATE TABLE conversation_responses (
    response_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    context_revision bigint NOT NULL CHECK (context_revision >= 1),
    model_id text NOT NULL CHECK (char_length(model_id) BETWEEN 1 AND 128),
    response_text text NOT NULL CHECK (char_length(response_text) BETWEEN 1 AND 12000),
    delivery_status text NOT NULL DEFAULT 'GENERATED'
        CHECK (delivery_status IN ('GENERATED','DELIVERED')),
    generated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    UNIQUE (conversation_id,response_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id),
    CHECK (
        (delivery_status='GENERATED' AND delivered_at IS NULL)
        OR (delivery_status='DELIVERED' AND delivered_at IS NOT NULL)
    )
);
CREATE INDEX conversation_responses_recent_idx
    ON conversation_responses(conversation_id,generated_at DESC);
