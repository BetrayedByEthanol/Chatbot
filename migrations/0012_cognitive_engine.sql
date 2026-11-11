-- P2-06: cognitive interpretations are lower-trust, source-linked proposals.
-- Conversation owns this independent progress watermark; summary watermark is separate.
ALTER TABLE conversations
    ADD COLUMN cognitive_watermark bigint NOT NULL DEFAULT 0
        CHECK (cognitive_watermark >= 0);

CREATE TABLE cognitive_jobs (
    operation_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    expected_generation bigint NOT NULL CHECK (expected_generation >= 1),
    start_sequence bigint NOT NULL CHECK (start_sequence >= 1),
    end_sequence bigint NOT NULL CHECK (end_sequence >= start_sequence),
    committed_at timestamptz,
    model_id text CHECK (model_id IS NULL OR char_length(model_id) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(conversation_id,start_sequence,expected_generation),
    UNIQUE(operation_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id)
);
CREATE INDEX cognitive_jobs_conversation_idx
    ON cognitive_jobs(conversation_id,start_sequence);

CREATE TABLE cognitive_reflections (
    reflection_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    evidence_turn_id uuid NOT NULL,
    evidence_quote text NOT NULL CHECK(char_length(evidence_quote) BETWEEN 1 AND 400),
    kind text NOT NULL CHECK(kind IN (
        'OBSERVATION','HYPOTHESIS','CONCLUSION','QUESTION','SELF_EVALUATION',
        'INTENTION'
    )),
    subject text NOT NULL CHECK(subject IN ('USER','CONVERSATION','PERSONA')),
    content text NOT NULL CHECK(char_length(content) BETWEEN 1 AND 600),
    confidence numeric(4,3) NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    epistemic_status text NOT NULL DEFAULT 'MODEL_INTERPRETED'
        CHECK(epistemic_status='MODEL_INTERPRETED'),
    review_status text NOT NULL DEFAULT 'PROPOSED'
        CHECK(review_status IN ('PROPOSED','SUPERSEDED','REJECTED')),
    model_id text NOT NULL CHECK(char_length(model_id) BETWEEN 1 AND 128),
    processed_through_sequence bigint NOT NULL CHECK(processed_through_sequence >= 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(operation_id,user_id,instance_id)
        REFERENCES cognitive_jobs(operation_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,evidence_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX cognitive_reflections_recent_idx
    ON cognitive_reflections(user_id,instance_id,created_at DESC);

-- Intention proposals are not permission to speak or to alter a goal.
-- Attention and the response scheduler must explicitly accept before delivery.
CREATE TABLE cognitive_intentions (
    intention_id uuid PRIMARY KEY,
    reflection_id uuid NOT NULL UNIQUE REFERENCES cognitive_reflections(reflection_id),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    trigger_text text NOT NULL CHECK(char_length(trigger_text) BETWEEN 1 AND 200),
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'PROPOSED'
        CHECK(status IN ('PROPOSED','ACCEPTED','EXPIRED','CANCELLED','FULFILLED')),
    already_asked boolean NOT NULL DEFAULT false,
    delivery_constraints jsonb NOT NULL DEFAULT
        '{"requires_attention_approval":true,"requires_response_scheduler":true}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(reflection_id) REFERENCES cognitive_reflections(reflection_id)
);
CREATE INDEX cognitive_intentions_pending_idx
    ON cognitive_intentions(user_id,instance_id,expires_at)
    WHERE status='PROPOSED';
