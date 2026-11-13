-- P4-02b remediation for legacy Task cancellation events lacking source_turn_id.
-- The Task owner requires a fresh authenticated, exact approval before binding
-- a historical source turn. No cancelled task is re-executed or modified.
CREATE TABLE task_cancellation_provenance (
    original_cancel_event_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    task_id uuid NOT NULL,
    original_conversation_id uuid NOT NULL,
    original_source_turn_id uuid NOT NULL,
    attestation_conversation_id uuid NOT NULL,
    attestation_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    proof_event_id uuid NOT NULL UNIQUE,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (original_cancel_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (proof_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY (original_conversation_id,original_source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (attestation_conversation_id,attestation_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (attestation_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    CHECK (attestation_turn_id <> approval_turn_id)
);
CREATE INDEX task_cancellation_provenance_source_idx
    ON task_cancellation_provenance(user_id,instance_id,original_source_turn_id);
