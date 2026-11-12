-- P3-02: bounded actionable work, dependency DAG, and durable user priority.
-- Deliberately NO task-owned operation queue admission or autonomous executor.
CREATE TABLE task_records (
    task_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    action_description text NOT NULL CHECK (char_length(action_description) BETWEEN 1 AND 800),
    completion_criterion text NOT NULL CHECK (char_length(completion_criterion) BETWEEN 1 AND 600),
    executor text NOT NULL CHECK (executor IN ('USER','AGENT')),
    evidence_policy text NOT NULL
        CHECK (evidence_policy IN ('USER_CONFIRMATION','OPERATION_RESULT')),
    capability text CHECK (capability IS NULL OR char_length(capability) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','USER_CONFIRMED','CANCELLED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    suspended boolean NOT NULL DEFAULT false,
    exclusive_only boolean NOT NULL DEFAULT false,
    preferred_over_task_id uuid,
    run_after_task_id uuid,
    deadline timestamptz,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,user_id,instance_id),
    CHECK (
        (executor='USER' AND evidence_policy='USER_CONFIRMATION' AND capability IS NULL)
        OR (executor='AGENT' AND evidence_policy='OPERATION_RESULT')
    ),
    CHECK (preferred_over_task_id IS NULL OR preferred_over_task_id<>task_id),
    CHECK (run_after_task_id IS NULL OR run_after_task_id<>task_id),
    FOREIGN KEY(user_id,instance_id)
        REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(preferred_over_task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(run_after_task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_records_goal_idx ON task_records(user_id,instance_id,goal_id,created_at);
CREATE INDEX task_records_status_idx ON task_records(user_id,instance_id,status,updated_at);
CREATE UNIQUE INDEX task_single_exclusive_per_instance
    ON task_records(user_id,instance_id) WHERE exclusive_only;

CREATE TABLE task_dependencies (
    task_id uuid NOT NULL,
    depends_on_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (task_id,depends_on_id),
    CHECK(task_id<>depends_on_id),
    FOREIGN KEY(task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(depends_on_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_dep_reverse_idx ON task_dependencies(user_id,instance_id,depends_on_id);

-- A user-confirmed action is NOT independently verified and never certifies
-- any AGENT task or operation completion.
CREATE TABLE task_completion_reports (
    report_id uuid PRIMARY KEY,
    task_id uuid NOT NULL UNIQUE,
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
    FOREIGN KEY(task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

CREATE TABLE task_priority_revisions (
    revision_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    task_version bigint NOT NULL CHECK (task_version >= 2),
    rule_kind text NOT NULL CHECK (rule_kind IN (
        'PREFER_OVER','RUN_AFTER','SUSPEND','RESUME',
        'SET_DEADLINE','CLEAR_DEADLINE','ONLY','CLEAR_ONLY')),
    related_task_id uuid,
    deadline timestamptz,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 300),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,task_version),
    FOREIGN KEY(task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(related_task_id,user_id,instance_id)
        REFERENCES task_records(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
