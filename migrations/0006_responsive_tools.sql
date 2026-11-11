-- Phase 1-02: durable bridge from a conversation turn to a P0 operation.
-- Admission is two-stage because authenticated enqueue owns its transaction.
CREATE TABLE conversation_tool_calls (
    tool_call_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    tool_name text NOT NULL CHECK (tool_name='text.statistics.v1'),
    tool_config jsonb NOT NULL,
    operation_id uuid UNIQUE REFERENCES operations(operation_id),
    admission_state text NOT NULL DEFAULT 'PREPARING'
        CHECK (admission_state IN ('PREPARING','QUEUED')),
    initial_reply_mode text CHECK (initial_reply_mode IN ('DIRECT','DEFERRED')),
    followup_state text NOT NULL DEFAULT 'PENDING'
        CHECK (followup_state IN ('PENDING','READY','DEFERRED','SUPPRESSED','DELIVERED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (conversation_id,turn_id)
        REFERENCES conversation_turns(conversation_id,turn_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    CHECK (admission_state='QUEUED' OR operation_id IS NULL)
);
CREATE INDEX conversation_tool_pending_idx
    ON conversation_tool_calls(user_id,instance_id,followup_state)
    WHERE followup_state IN ('PENDING','READY','DEFERRED');
CREATE TABLE conversation_tool_results (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    claim_generation bigint NOT NULL CHECK (claim_generation >= 1),
    result_text text NOT NULL CHECK (char_length(result_text) BETWEEN 1 AND 12000),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(operation_id,claim_generation)
);
