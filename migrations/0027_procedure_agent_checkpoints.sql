-- P4-05b: immutable, bounded P2 read-only safe-stop checkpoints.
-- A record is evidence for data restoration, NEVER renewed execution authority.
-- The current P0 operation checkpoint_ref points to this source-owned row.
CREATE TABLE procedure_agent_checkpoints (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    claim_generation bigint NOT NULL CHECK (claim_generation >= 1),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    task_id uuid NOT NULL,
    run_id uuid NOT NULL,
    step_id text NOT NULL,
    task_version bigint NOT NULL CHECK (task_version >= 1),
    run_version bigint NOT NULL CHECK (run_version >= 1),
    step_version bigint NOT NULL CHECK (step_version >= 1),
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    background_epoch bigint NOT NULL CHECK (background_epoch >= 1),
    capability text NOT NULL CHECK (capability = 'text.statistics.v1'),
    max_words integer NOT NULL CHECK (max_words BETWEEN 1 AND 20),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    boundary text NOT NULL CHECK (boundary = 'READ_ONLY_RESTART_REQUIRED'),
    instruction_sha256 text NOT NULL CHECK (instruction_sha256 ~ '^[0-9a-f]{64}$'),
    checkpoint_ref text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (operation_id,claim_generation),
    FOREIGN KEY (user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY (task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY (run_id,step_id,user_id,instance_id)
        REFERENCES procedure_run_steps(run_id,step_id,user_id,instance_id)
);

CREATE TRIGGER immutable_procedure_agent_checkpoints
    BEFORE UPDATE OR DELETE ON procedure_agent_checkpoints
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
