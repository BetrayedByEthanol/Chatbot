-- MVP-06B: delivered guided-dialogue bindings are not step outcomes.
-- The ProcedureRun owner remains authoritative over progress.
CREATE TABLE procedure_guidance_responses (
    response_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    turn_id uuid NOT NULL,
    run_id uuid NOT NULL,
    run_version bigint NOT NULL CHECK(run_version>=1),
    step_id text,
    step_version bigint CHECK(step_version>=1),
    kind text NOT NULL CHECK(kind IN ('GUIDE','REVIEW','REPORT','PAUSE','RESUME','CANCEL','STATUS')),
    bound_at timestamptz,
    CHECK((step_id IS NULL)=(step_version IS NULL)),
    CHECK(kind NOT IN ('GUIDE','REVIEW','REPORT') OR step_id IS NOT NULL),
    CHECK(kind NOT IN ('PAUSE','RESUME','CANCEL') OR step_id IS NULL),
    FOREIGN KEY(response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(conversation_id,turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    FOREIGN KEY(run_id,step_id,user_id,instance_id)
        REFERENCES procedure_run_steps(run_id,step_id,user_id,instance_id)
);
CREATE INDEX procedure_guidance_turn_idx
    ON procedure_guidance_responses(conversation_id,turn_id,user_id,instance_id);
