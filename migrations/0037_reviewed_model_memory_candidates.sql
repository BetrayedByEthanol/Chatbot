-- MVP-02C.2: lower-trust model proposals require authenticated review
-- before any authoritative Memory write. Distinct from deterministic
-- memory_extraction_candidates, which may auto-admit exact safe reports.
CREATE TABLE memory_model_candidates (
    candidate_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    model_id text NOT NULL CHECK (char_length(model_id) BETWEEN 1 AND 128),
    memory_kind text NOT NULL CHECK (memory_kind='PREFERENCE'),
    subject_key text NOT NULL CHECK (subject_key='user'),
    predicate_key text NOT NULL CHECK (char_length(predicate_key) BETWEEN 1 AND 64),
    value_text text NOT NULL CHECK (value_text IN ('yes','no')),
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 320),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status text NOT NULL DEFAULT 'PENDING_REVIEW'
      CHECK (status IN ('PENDING_REVIEW','APPROVED','REJECTED')),
    approved_memory_id uuid,
    approved_version bigint,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reviewed_at timestamptz,
    UNIQUE (source_turn_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
      REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
      REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (approved_memory_id,user_id,instance_id)
      REFERENCES memory_records(memory_id,user_id,instance_id),
    CHECK ((approved_memory_id IS NULL) = (approved_version IS NULL)),
    CHECK ((status='APPROVED') = (approved_memory_id IS NOT NULL))
);
CREATE INDEX memory_model_candidates_review_idx
  ON memory_model_candidates(user_id,instance_id,status,created_at DESC);
