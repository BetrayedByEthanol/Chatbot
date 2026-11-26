-- MVP-06A: Goal-owned conversational proposals, never executable goals.
-- A version's objective and displayed criteria remain immutable; status alone
-- transitions. A revised proposal uses the same goal ID and a new version.
CREATE TABLE goal_conversation_candidates (
    goal_id uuid NOT NULL,
    version integer NOT NULL CHECK(version BETWEEN 1 AND 12),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK(selection_generation>=1),
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    proposal_turn_id uuid NOT NULL,
    proposal_response_id uuid NOT NULL UNIQUE,
    proposal_sequence bigint NOT NULL CHECK(proposal_sequence>=1),
    title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 120),
    desired_outcome text NOT NULL CHECK(char_length(desired_outcome) BETWEEN 1 AND 120),
    success_criterion text CHECK(char_length(success_criterion) BETWEEN 1 AND 160),
    horizon text NOT NULL CHECK(horizon='OPEN'),
    status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','SUPERSEDED','REJECTED','APPROVED')),
    decision_turn_id uuid,
    created_event_id uuid NOT NULL,
    decision_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(goal_id,version),
    UNIQUE(user_id,instance_id,conversation_id,proposal_turn_id),
    CHECK((status='PENDING')=(decision_turn_id IS NULL)),
    CHECK((status='PENDING')=(decision_event_id IS NULL)),
    CHECK(status<>'APPROVED' OR success_criterion IS NOT NULL),
    FOREIGN KEY(conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,proposal_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,decision_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(proposal_response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(decision_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE UNIQUE INDEX goal_conversation_one_pending_idx
    ON goal_conversation_candidates(user_id,instance_id,conversation_id)
    WHERE status='PENDING';
CREATE INDEX goal_conversation_candidate_id_idx
    ON goal_conversation_candidates(user_id,instance_id,goal_id);
