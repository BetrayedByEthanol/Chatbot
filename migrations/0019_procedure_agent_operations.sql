-- P3-04 execution gate: explicit user-reviewed authorization for ONE
-- registered READ_ONLY tool, bound to an exact Task, Run, Step and versions.
-- The P0 scheduler remains the only operation scheduler.
CREATE TABLE procedure_agent_operation_authorizations (
    operation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    run_id uuid NOT NULL,
    step_id text NOT NULL,
    task_id uuid NOT NULL,
    run_version bigint NOT NULL CHECK(run_version>=1),
    step_version bigint NOT NULL CHECK(step_version>=1),
    task_version bigint NOT NULL CHECK(task_version>=1),
    selection_generation bigint NOT NULL CHECK(selection_generation>=1),
    capability text NOT NULL CHECK(capability='text.statistics.v1'),
    max_words integer NOT NULL CHECK(max_words BETWEEN 1 AND 20),
    state text NOT NULL DEFAULT 'AUTHORIZED'
        CHECK(state IN ('AUTHORIZED','RECONCILED','INVALIDATED')),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    authorization_event_id uuid NOT NULL,
    result_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    FOREIGN KEY(run_id,step_id,user_id,instance_id)
        REFERENCES procedure_run_steps(run_id,step_id,user_id,instance_id),
    FOREIGN KEY(task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(authorization_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(result_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE UNIQUE INDEX procedure_agent_one_pending_per_step
    ON procedure_agent_operation_authorizations(run_id,step_id)
    WHERE state='AUTHORIZED';
CREATE INDEX procedure_agent_authority_scope_idx
    ON procedure_agent_operation_authorizations(user_id,instance_id,task_id);

-- Only authenticated stage admission may attach a task to an operation.
ALTER TABLE operations
    ADD CONSTRAINT operation_task_scoped_fk
    FOREIGN KEY(task_id,user_id,instance_id)
    REFERENCES goal_tasks(task_id,user_id,instance_id);

CREATE TABLE procedure_agent_operation_results (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    claim_generation bigint NOT NULL CHECK(claim_generation>=1),
    result_text text NOT NULL CHECK(char_length(result_text) BETWEEN 1 AND 12000),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(operation_id,claim_generation)
);

-- A verified result is a trusted operation outcome (not guaranteed goal
-- attainment). Unknown effects are never treated as verified.
ALTER TABLE procedure_run_steps DROP CONSTRAINT procedure_run_steps_status_check;
ALTER TABLE procedure_run_steps ADD CONSTRAINT procedure_run_steps_status_check
    CHECK(status IN (
        'NOT_STARTED','INSTRUCTED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','NEEDS_REVIEW',
        'VERIFIED','OUTCOME_UNKNOWN'));
ALTER TABLE procedure_run_step_events
    DROP CONSTRAINT procedure_run_step_events_kind_check;
ALTER TABLE procedure_run_step_events
    ADD CONSTRAINT procedure_run_step_events_kind_check
    CHECK(kind IN (
        'INSTRUCTION_DELIVERED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','CORRECTED',
        'VERIFIED','OUTCOME_UNKNOWN'));
ALTER TABLE procedure_run_step_events
    DROP CONSTRAINT procedure_run_step_events_old_status_check;
ALTER TABLE procedure_run_step_events
    ADD CONSTRAINT procedure_run_step_events_old_status_check
    CHECK(old_status IN (
        'NOT_STARTED','INSTRUCTED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','NEEDS_REVIEW',
        'VERIFIED','OUTCOME_UNKNOWN'));
ALTER TABLE procedure_run_step_events
    DROP CONSTRAINT procedure_run_step_events_new_status_check;
ALTER TABLE procedure_run_step_events
    ADD CONSTRAINT procedure_run_step_events_new_status_check
    CHECK(new_status IN (
        'NOT_STARTED','INSTRUCTED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','NEEDS_REVIEW',
        'VERIFIED','OUTCOME_UNKNOWN'));

-- Durable method evidence cannot be rewritten after a worker restart.
CREATE TRIGGER immutable_procedure_agent_results
    BEFORE UPDATE OR DELETE ON procedure_agent_operation_results
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
