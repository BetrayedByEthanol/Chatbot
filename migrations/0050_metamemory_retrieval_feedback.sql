-- MVP-05D.5: recall observations are evidence about retrieval, not facts.
ALTER TABLE conversation_responses ADD CONSTRAINT conversation_response_meta_scope_uq
    UNIQUE(response_id,user_id,instance_id);
CREATE TABLE memory_recall_observations (
    response_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL,
    route text NOT NULL CHECK(route IN ('FACT','MEANING','ENTITY','ENTITY_HISTORY','PATTERN','KNOWLEDGE')),
    query_key text NOT NULL CHECK(query_key ~ '^[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK(outcome IN (
        'AVAILABLE','UNKNOWN','AMBIGUOUS','CONTRADICTORY','UNVERIFIABLE','STALE','NO_EVIDENCE','BUDGET'
    )),
    evidence_refs jsonb NOT NULL CHECK(jsonb_typeof(evidence_refs)='array'
                                      AND jsonb_array_length(evidence_refs)<=16),
    clarification_key text CHECK(clarification_key IS NULL OR clarification_key ~ '^[0-9a-f]{64}$'),
    clarification_suppressed boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK(NOT clarification_suppressed OR clarification_key IS NOT NULL),
    CHECK(clarification_key IS NULL OR outcome='AMBIGUOUS'),
    UNIQUE(response_id,user_id,instance_id),
    FOREIGN KEY(response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX memory_recall_observations_query_idx
    ON memory_recall_observations(user_id,instance_id,route,query_key,observed_at DESC,response_id);
CREATE TABLE memory_retrieval_feedback (
    feedback_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    response_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    feedback_turn_id uuid NOT NULL UNIQUE,
    outcome text NOT NULL CHECK(outcome IN ('RELEVANT','IRRELEVANT','INCORRECT','MISSED')),
    passage_ordinal integer CHECK(passage_ordinal BETWEEN 1 AND 8),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(response_id,passage_ordinal),
    FOREIGN KEY(response_id,user_id,instance_id)
        REFERENCES memory_recall_observations(response_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,feedback_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE UNIQUE INDEX memory_retrieval_feedback_whole_response_uq
    ON memory_retrieval_feedback(response_id) WHERE passage_ordinal IS NULL;
CREATE INDEX memory_retrieval_feedback_scope_idx
    ON memory_retrieval_feedback(user_id,instance_id,recorded_at DESC);
-- Metadata is intentionally not a historical eligibility revision writer.
-- Recall commits can hold the existing historical SHARE lock; feedback never
-- changes source eligibility, truth, confidence or stored owner revisions.
