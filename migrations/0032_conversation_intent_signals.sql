-- MVP-01A: tentative, source-linked conversation interpretation signals.
-- These are Conversation Manager discussion cues, never approved goals,
-- authoritative instructions, permissions, memory facts or work outcomes.
CREATE TABLE conversation_intent_signals (
    signal_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    turn_sequence bigint NOT NULL CHECK (turn_sequence >= 1),
    source_event_id uuid NOT NULL,
    intent_kind text NOT NULL CHECK (
        intent_kind IN ('OBJECTIVE','REQUEST','INSTRUCTION','CORRECTION',
                        'OBSERVATION','HYPOTHETICAL')
    ),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 200),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    method_revision text NOT NULL CHECK (
        char_length(method_revision) BETWEEN 1 AND 64
    ),
    epistemic_status text NOT NULL DEFAULT 'RULE_INFERRED'
        CHECK (epistemic_status='RULE_INFERRED'),
    proposal_status text NOT NULL DEFAULT 'UNREVIEWED'
        CHECK (proposal_status='UNREVIEWED'),
    related_signal_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id,signal_id),
    UNIQUE (conversation_id,signal_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (conversation_id,related_signal_id,user_id,instance_id)
        REFERENCES conversation_intent_signals(
            conversation_id,signal_id,user_id,instance_id
        ),
    CHECK (related_signal_id IS NULL OR related_signal_id<>signal_id)
);
CREATE INDEX conversation_intent_recent_idx
    ON conversation_intent_signals (
        conversation_id,turn_sequence DESC
    );
CREATE INDEX conversation_intent_scope_idx
    ON conversation_intent_signals (
        user_id,instance_id,intent_kind,created_at DESC
    );
