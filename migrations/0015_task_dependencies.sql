-- P3-02: scoped Goal/Task domain state; no autonomous operation dispatch.
CREATE TABLE goal_tasks (
    task_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    action_description text NOT NULL CHECK (char_length(action_description) BETWEEN 1 AND 800),
    completion_criterion text NOT NULL CHECK (char_length(completion_criterion) BETWEEN 1 AND 800),
    evidence_requirement text NOT NULL CHECK (char_length(evidence_requirement) BETWEEN 1 AND 800),
    executor text NOT NULL CHECK (executor IN ('USER','AGENT')),
    evidence_policy text NOT NULL CHECK (evidence_policy IN ('USER_CONFIRMATION','OPERATION_RESULT')),
    capability text,
    status text NOT NULL DEFAULT 'PLANNED'
        CHECK (status IN ('PLANNED','USER_REPORTED_DONE','USER_CONFIRMED','CANCELLED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    suspended boolean NOT NULL DEFAULT false,
    exclusive_only boolean NOT NULL DEFAULT false,
    preferred_over_task_id uuid,
    run_after_task_id uuid,
    deadline timestamptz,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,goal_id,user_id,instance_id),
    UNIQUE(task_id,user_id,instance_id),
    CHECK ((executor='USER' AND evidence_policy='USER_CONFIRMATION'
              AND capability IS NULL)
        OR (executor='AGENT' AND evidence_policy='OPERATION_RESULT'
            AND char_length(capability) BETWEEN 1 AND 120)),
    CHECK (preferred_over_task_id IS NULL OR preferred_over_task_id<>task_id),
    CHECK (run_after_task_id IS NULL OR run_after_task_id<>task_id),
    CHECK (status NOT IN ('CANCELLED','USER_CONFIRMED') OR NOT exclusive_only),
    FOREIGN KEY(goal_id,user_id,instance_id) REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(preferred_over_task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(run_after_task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_tasks_goal_idx ON goal_tasks(user_id,instance_id,goal_id,created_at);
CREATE UNIQUE INDEX task_one_exclusive_per_persona ON goal_tasks(user_id,instance_id) WHERE exclusive_only;

CREATE TABLE task_dependencies (
    task_id uuid NOT NULL,
    prerequisite_task_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(task_id,prerequisite_task_id),
    CHECK(task_id<>prerequisite_task_id),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(prerequisite_task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_dependency_forward_idx ON task_dependencies(prerequisite_task_id);

CREATE TABLE task_outcome_reports (
    report_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    report text NOT NULL CHECK(report IN ('DONE','OBSTACLE')),
    epistemic_status text NOT NULL DEFAULT 'USER_REPORTED'
        CHECK(epistemic_status='USER_REPORTED'),
    classification_status text NOT NULL DEFAULT 'UNREVIEWED'
        CHECK(classification_status='UNREVIEWED'),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,source_turn_id,report),
    UNIQUE(report_id,task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);

-- User-confirmed is separate from an unreviewed report and never independent verification.
CREATE TABLE task_user_confirmations (
    confirmation_id uuid PRIMARY KEY,
    report_id uuid NOT NULL UNIQUE,
    task_id uuid NOT NULL UNIQUE,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    task_version bigint NOT NULL CHECK (task_version >= 2),
    epistemic_status text NOT NULL DEFAULT 'USER_CONFIRMED'
        CHECK (epistemic_status='USER_CONFIRMED'),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(report_id,task_id,goal_id,user_id,instance_id)
        REFERENCES task_outcome_reports(report_id,task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);

-- Append-only explicit user priority history. Latest rule for a given slot wins.
CREATE TABLE task_priority_rules (
    priority_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    rule text NOT NULL CHECK(rule IN ('PREFER_OVER','CLEAR_PREFERENCE','SUSPEND','RESUME','SET_DEADLINE','CLEAR_DEADLINE','RUN_AFTER','CLEAR_RUN_AFTER','ONLY','CLEAR_ONLY')),
    other_task_id uuid,
    deadline timestamptz,
    task_version bigint NOT NULL,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((rule IN ('PREFER_OVER','RUN_AFTER') AND other_task_id IS NOT NULL AND deadline IS NULL AND other_task_id<>task_id)
        OR (rule='SET_DEADLINE' AND deadline IS NOT NULL AND other_task_id IS NULL)
        OR (rule IN ('CLEAR_PREFERENCE','SUSPEND','RESUME','CLEAR_DEADLINE','CLEAR_RUN_AFTER','ONLY','CLEAR_ONLY') AND other_task_id IS NULL AND deadline IS NULL)),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id) REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(other_task_id,user_id,instance_id) REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_priority_latest_idx ON task_priority_rules(task_id,rule,created_at DESC);
