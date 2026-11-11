-- P1-03: conversation-owned, minimal revisioned dialogue state.
-- These records are interpretations / presentation progress, never task truth.
CREATE TABLE dialogue_threads (
    thread_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    attention_status text NOT NULL DEFAULT 'FOCUSED'
        CHECK (attention_status IN ('FOCUSED','RECENT','DORMANT')),
    resolution_status text NOT NULL DEFAULT 'OPEN'
        CHECK (resolution_status IN ('OPEN','RESOLVED','ABANDONED')),
    next_conversational_action text
        CHECK (next_conversational_action IS NULL OR
               char_length(next_conversational_action) BETWEEN 1 AND 200),
    origin_turn_id uuid NOT NULL,
    last_turn_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id,thread_id),
    UNIQUE (conversation_id,thread_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,origin_turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id),
    FOREIGN KEY (conversation_id,last_turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id)
);

ALTER TABLE conversations ADD COLUMN focus_thread_id uuid;
ALTER TABLE conversations ADD CONSTRAINT conversation_focus_thread_fk
    FOREIGN KEY (conversation_id,focus_thread_id)
    REFERENCES dialogue_threads(conversation_id,thread_id);

CREATE TABLE dialogue_turn_links (
    turn_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (conversation_id,turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id),
    FOREIGN KEY (conversation_id,thread_id,user_id,instance_id)
        REFERENCES dialogue_threads(conversation_id,thread_id,user_id,instance_id)
);
CREATE INDEX dialogue_turn_links_thread_idx ON dialogue_turn_links(thread_id);

CREATE TABLE dialogue_pending_acts (
    act_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('QUESTION','DECISION','ACKNOWLEDGMENT')),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 200),
    source_response_id uuid NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RESOLVED','CANCELLED')),
    resolution_kind text CHECK (resolution_kind IN ('ANSWERED','CONFIRMED','DECLINED')),
    resolution_turn_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (conversation_id,thread_id,user_id,instance_id)
        REFERENCES dialogue_threads(conversation_id,thread_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,source_response_id)
        REFERENCES conversation_responses(conversation_id,response_id),
    FOREIGN KEY (conversation_id,resolution_turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id),
    CHECK (
        (status='PENDING' AND resolution_kind IS NULL AND resolution_turn_id IS NULL)
        OR (status='RESOLVED' AND resolution_kind IS NOT NULL AND resolution_turn_id IS NOT NULL)
        OR (status='CANCELLED' AND resolution_kind IS NULL AND resolution_turn_id IS NOT NULL)
    )
);
CREATE INDEX dialogue_pending_by_thread_idx
    ON dialogue_pending_acts(thread_id,created_at)
    WHERE status='PENDING';

CREATE TABLE dialogue_referents (
    referent_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('TOPIC','ENTITY')),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 160),
    source_turn_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (conversation_id,thread_id,user_id,instance_id)
        REFERENCES dialogue_threads(conversation_id,thread_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,source_turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id)
);
CREATE INDEX dialogue_referents_by_thread_idx
    ON dialogue_referents(thread_id,created_at DESC);

-- The conversation row's revision already fences prompt context; no
-- independent materialized snapshot or telemetry write is required.
