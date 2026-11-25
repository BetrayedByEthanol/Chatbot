-- MVP-05A: immutable ingress-time context on the existing Conversation turn.
-- Do not fabricate historical timezone/relative resolution for old rows.
ALTER TABLE conversation_turns
    ADD COLUMN temporal_context jsonb
        CHECK (temporal_context IS NULL OR jsonb_typeof(temporal_context)='object');

-- Temporal anchors are primary-user inputs, not assistant outputs or timers.
CREATE INDEX conversation_turns_temporal_scope_idx
    ON conversation_turns(user_id,instance_id,created_at,turn_id)
    WHERE source_input_event_id IS NOT NULL;
