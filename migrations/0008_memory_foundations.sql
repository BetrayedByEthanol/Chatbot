-- P2-01: scoped user-reported facts, preferences and personal meanings.
-- Conversation turns remain immutable evidence; this table is not task state.
ALTER TABLE conversation_turns
    ADD CONSTRAINT conversation_turn_evidence_scope_uq
    UNIQUE (conversation_id,turn_id,user_id,instance_id);

CREATE TABLE memory_records (
    memory_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    memory_kind text NOT NULL CHECK (
        memory_kind IN ('FACT','PREFERENCE','PERSONAL_MEANING')
    ),
    subject_key text NOT NULL CHECK (char_length(subject_key) BETWEEN 1 AND 64),
    predicate_key text NOT NULL CHECK (char_length(predicate_key) BETWEEN 1 AND 64),
    value_text text NOT NULL CHECK (char_length(value_text) BETWEEN 1 AND 2000),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    epistemic_status text NOT NULL DEFAULT 'USER_REPORTED'
        CHECK (epistemic_status='USER_REPORTED'),
    privacy_class text NOT NULL DEFAULT 'NORMAL'
        CHECK (privacy_class IN ('NORMAL','SENSITIVE')),
    record_status text NOT NULL DEFAULT 'ACTIVE'
        CHECK (record_status IN ('ACTIVE','RETRACTED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 2000),
    source_id text NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (memory_id,user_id,instance_id),
    UNIQUE (user_id,instance_id,memory_kind,subject_key,predicate_key),
    FOREIGN KEY (user_id,instance_id)
        REFERENCES persona_instances(user_id,id),
    FOREIGN KEY (source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);

CREATE TABLE memory_revisions (
    memory_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version >= 1),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('ADMIT','CORRECT','RETRACT')),
    value_text text NOT NULL,
    record_status text NOT NULL CHECK (record_status IN ('ACTIVE','RETRACTED')),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    source_quote text NOT NULL,
    source_id text NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_id,version),
    FOREIGN KEY (memory_id,user_id,instance_id)
        REFERENCES memory_records(memory_id,user_id,instance_id),
    FOREIGN KEY (source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

CREATE INDEX memory_records_lookup_idx
    ON memory_records (user_id,instance_id,memory_kind,source_turn_at DESC)
    WHERE record_status='ACTIVE';
CREATE INDEX memory_records_search_idx
    ON memory_records USING gin (
        to_tsvector('simple',replace(subject_key,'_',' ') || ' ' ||
            replace(predicate_key,'_',' ') || ' ' || value_text)
    ) WHERE record_status='ACTIVE';
