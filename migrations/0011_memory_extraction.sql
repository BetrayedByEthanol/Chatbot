-- P2-04: bounded explicit-claim extraction with provenance and consolidation.
-- Candidate decisions do not change authoritative memory unless semantic validation
-- admits an unoccupied slot. Conflicting values remain reviewable, not corrections.
CREATE TABLE memory_extraction_candidates (
    candidate_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    memory_kind text NOT NULL CHECK (
        memory_kind IN ('FACT','PREFERENCE','PERSONAL_MEANING')
    ),
    subject_key text NOT NULL CHECK (char_length(subject_key) BETWEEN 1 AND 64),
    predicate_key text NOT NULL CHECK (char_length(predicate_key) BETWEEN 1 AND 64),
    value_text text NOT NULL CHECK (char_length(value_text) BETWEEN 1 AND 2000),
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 2000),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status text NOT NULL CHECK (
        status IN ('PENDING','ADMITTED','CORROBORATED','CONFLICT','STALE','APPROVED')
    ),
    target_memory_id uuid,
    target_version bigint,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reviewed_at timestamptz,
    UNIQUE (source_turn_id,memory_kind,subject_key,predicate_key),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (target_memory_id,user_id,instance_id)
        REFERENCES memory_records(memory_id,user_id,instance_id),
    CHECK ((target_memory_id IS NULL) = (target_version IS NULL)),
    CHECK (status NOT IN ('ADMITTED','CORROBORATED','APPROVED')
           OR target_memory_id IS NOT NULL)
);
CREATE INDEX memory_extraction_candidates_review_idx
    ON memory_extraction_candidates(user_id,instance_id,status,created_at DESC);

-- Additional evidence corroborates a specific immutable memory revision.
-- A later correction cannot silently inherit support for an older value.
CREATE TABLE memory_claim_support (
    memory_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version >= 1),
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 2000),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_id,version,source_turn_id),
    FOREIGN KEY (memory_id,version)
        REFERENCES memory_revisions(memory_id,version),
    FOREIGN KEY (memory_id,user_id,instance_id)
        REFERENCES memory_records(memory_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
