-- MVP-01B: model-assisted suggestions remain tentative Conversation-owned cues.
-- A model result is only admitted alongside a committed, fresh conversation
-- response. Neither the suggestion nor its score grants goal/tool authority.
ALTER TABLE conversation_turns
    ADD COLUMN source_input_event_id uuid;
ALTER TABLE conversation_turns
    ADD CONSTRAINT conversation_turns_source_input_event_fk
    FOREIGN KEY (source_input_event_id,user_id,instance_id)
    REFERENCES events(event_id,user_id,instance_id);

ALTER TABLE conversation_intent_signals
    DROP CONSTRAINT conversation_intent_signals_epistemic_status_check;
ALTER TABLE conversation_intent_signals
    ADD CONSTRAINT conversation_intent_signals_epistemic_status_check
    CHECK (epistemic_status IN ('RULE_INFERRED','MODEL_PROPOSED'));

ALTER TABLE conversation_intent_signals
    ADD COLUMN source_response_id uuid;
ALTER TABLE conversation_intent_signals
    ADD CONSTRAINT conversation_intent_response_fk
    FOREIGN KEY (conversation_id,source_response_id)
    REFERENCES conversation_responses(conversation_id,response_id);
ALTER TABLE conversation_intent_signals
    ADD CONSTRAINT conversation_intent_source_kind_check
    CHECK (
        (epistemic_status='RULE_INFERRED' AND source_response_id IS NULL)
        OR
        (epistemic_status='MODEL_PROPOSED' AND source_response_id IS NOT NULL)
    );
