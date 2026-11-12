-- Phase 3 / P3-01: domain-owned user-approved outcomes and strategy proposals.
-- No Goal Engine autonomous executor is authorized by this migration.
CREATE TABLE goal_records (
    goal_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    parent_goal_id uuid,
    depth integer NOT NULL CHECK (depth BETWEEN 0 AND 5),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    desired_outcome text NOT NULL CHECK (char_length(desired_outcome) BETWEEN 1 AND 600),
    success_criterion text NOT NULL CHECK (char_length(success_criterion) BETWEEN 1 AND 600),
    horizon text NOT NULL CHECK (horizon IN ('SHORT','MEDIUM','LONG','OPEN')),
    target_at timestamptz,
    evaluation_start_at timestamptz,
    evaluation_end_at timestamptz,
    privacy_class text NOT NULL DEFAULT 'INTERNAL' CHECK(privacy_class='INTERNAL'),
    status text NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','PAUSED','ABANDONED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (goal_id,user_id,instance_id),
    CHECK ((parent_goal_id IS NULL AND depth=0)
        OR (parent_goal_id IS NOT NULL AND depth>0)),
    CHECK (parent_goal_id IS NULL OR parent_goal_id<>goal_id),
    CHECK (evaluation_start_at IS NULL OR evaluation_end_at IS NULL
           OR evaluation_end_at>evaluation_start_at),
    FOREIGN KEY(user_id,instance_id)
        REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(parent_goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_records_tree_idx
    ON goal_records(user_id,instance_id,parent_goal_id,created_at);
CREATE INDEX goal_records_status_idx
    ON goal_records(user_id,instance_id,status,updated_at);

CREATE TABLE goal_evidence (
    evidence_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    evidence_kind text NOT NULL CHECK (
        evidence_kind IN ('PROGRESS','OBSTACLE','SUCCESS_REPORTED')),
    epistemic_status text NOT NULL DEFAULT 'USER_REPORTED'
        CHECK(epistemic_status='USER_REPORTED'),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    goal_version bigint NOT NULL CHECK (goal_version >= 1),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (goal_id,source_turn_id,evidence_kind),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_evidence_recent_idx ON goal_evidence(goal_id,recorded_at DESC);

-- Strategy proposals are never implicitly selected or treated as procedures.
CREATE TABLE goal_strategies (
    strategy_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    approach text NOT NULL CHECK (char_length(approach) BETWEEN 1 AND 600),
    suitability_note text NOT NULL CHECK (char_length(suitability_note) BETWEEN 1 AND 400),
    status text NOT NULL DEFAULT 'CANDIDATE'
        CHECK(status='CANDIDATE'),
    origin text NOT NULL DEFAULT 'USER_REQUESTED'
        CHECK(origin='USER_REQUESTED'),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_strategies_by_goal_idx
    ON goal_strategies(goal_id,created_at,strategy_id);

-- Selection is append-only. No selection can change the approved outcome.
CREATE TABLE goal_strategy_selections (
    selection_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    strategy_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    goal_version bigint NOT NULL CHECK(goal_version>=2),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (goal_id,goal_version),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(strategy_id,goal_id,user_id,instance_id)
        REFERENCES goal_strategies(strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_strategy_selected_idx
    ON goal_strategy_selections(goal_id,goal_version DESC);

CREATE TABLE goal_status_revisions (
    revision_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    goal_version bigint NOT NULL,
    old_status text NOT NULL,
    new_status text NOT NULL,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(goal_id,goal_version),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
