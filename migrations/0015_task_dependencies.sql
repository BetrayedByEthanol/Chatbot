-- P3-02: scoped Goal/Task domain state; no autonomous operation dispatch.
CREATE TABLE goal_tasks (
    task_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    completion_criterion text NOT NULL CHECK (char_length(completion_criterion) BETWEEN 1 AND 800),
    evidence_requirement text NOT NULL CHECK (char_length(evidence_requirement) BETWEEN 1 AND 800),
    executor text NOT NULL CHECK (executor IN ('USER','AGENT')),
    capability text,
    status text NOT NULL DEFAULT 'PLANNED'
        CHECK (status IN ('PLANNED','USER_REPORTED_DONE','CANCELLED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,goal_id,user_id,instance_id),
    CHECK ((executor='USER' AND capability IS NULL)
        OR (executor='AGENT' AND char_length(capability) BETWEEN 1 AND 120)),
    FOREIGN KEY(goal_id,user_id,instance_id) REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_tasks_goal_idx ON goal_tasks(user_id,instance_id,goal_id,created_at);

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
    FOREIGN KEY(prerequisite_task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
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
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,source_turn_id,report),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
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
    rule text NOT NULL CHECK(rule IN ('PREFER_OVER','SUSPEND','SET_DEADLINE','RUN_AFTER')),
    other_task_id uuid,
    deadline timestamptz,
    task_version bigint NOT NULL,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((rule IN ('PREFER_OVER','RUN_AFTER') AND other_task_id IS NOT NULL AND deadline IS NULL AND other_task_id<>task_id)
        OR (rule='SET_DEADLINE' AND deadline IS NOT NULL AND other_task_id IS NULL)
        OR (rule='SUSPEND' AND other_task_id IS NULL AND deadline IS NULL)),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id) REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(other_task_id,goal_id,user_id,instance_id) REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_priority_latest_idx ON task_priority_rules(task_id,rule,created_at DESC);
