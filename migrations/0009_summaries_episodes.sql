-- P2-02: summaries compact completed conversation ranges; episodes are
-- separate, evidence-anchored interpretations, not verified user facts.
CREATE TABLE conversation_summaries (
    summary_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    start_sequence bigint NOT NULL CHECK (start_sequence >= 1),
    end_sequence bigint NOT NULL CHECK (end_sequence >= start_sequence),
    first_turn_id uuid NOT NULL,
    last_turn_id uuid NOT NULL,
    source_digest char(64) NOT NULL,
    summary_text text NOT NULL CHECK (char_length(summary_text) BETWEEN 1 AND 4000),
    model_id text NOT NULL CHECK (char_length(model_id) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id,start_sequence),
    UNIQUE (conversation_id,end_sequence),
    UNIQUE (summary_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,first_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,last_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX conversation_summaries_latest_idx
    ON conversation_summaries(conversation_id,end_sequence DESC);

CREATE TABLE memory_episodes (
    episode_id uuid PRIMARY KEY,
    summary_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 1),
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 1000),
    occurred_at timestamptz NOT NULL,
    topic text NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 120),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1200),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    epistemic_status text NOT NULL DEFAULT 'MODEL_INTERPRETED'
        CHECK (epistemic_status = 'MODEL_INTERPRETED'),
    privacy_class text NOT NULL DEFAULT 'NORMAL'
        CHECK (privacy_class = 'NORMAL'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (summary_id,user_id,instance_id)
        REFERENCES conversation_summaries(summary_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX memory_episodes_topic_idx ON memory_episodes
    USING gin (to_tsvector('simple',topic || ' ' || description));
CREATE INDEX memory_episodes_scope_time_idx
    ON memory_episodes(user_id,instance_id,occurred_at DESC);

-- A durable correlation record is staged before the existing P0 operation
-- is enqueued; a crash between stages is repaired by idempotent rescheduling.
CREATE TABLE conversation_summary_jobs (
    operation_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    expected_generation bigint NOT NULL CHECK (expected_generation >= 1),
    start_sequence bigint NOT NULL CHECK (start_sequence >= 1),
    end_sequence bigint NOT NULL CHECK (end_sequence >= start_sequence),
    summary_id uuid REFERENCES conversation_summaries(summary_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id,start_sequence),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id)
);
