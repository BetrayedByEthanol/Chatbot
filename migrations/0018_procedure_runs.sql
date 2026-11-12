-- P3-04: durable ProcedureRun progress. No scheduler admission or Task writes.
-- Historical template/version and personal-revision pins are never repointed.

ALTER TABLE personalized_procedure_revisions
    ADD CONSTRAINT personalized_revision_scoped_run_fk
    UNIQUE(variant_id,revision,user_id,instance_id);

CREATE TABLE procedure_runs (
    run_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    template_id uuid NOT NULL,
    template_version integer NOT NULL CHECK(template_version>=1),
    variant_id uuid,
    variant_revision integer CHECK(variant_revision>=1),
    task_id uuid,
    status text NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','PAUSED','NEEDS_REVIEW',
                         'USER_CONFIRMED','CANCELLED')),
    version bigint NOT NULL DEFAULT 1 CHECK(version>=1),
    steps_sha256 text NOT NULL CHECK(steps_sha256 ~ '^[0-9a-f]{64}$'),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((variant_id IS NULL)=(variant_revision IS NULL)),
    UNIQUE(run_id,user_id,instance_id),
    FOREIGN KEY(user_id,instance_id,persona_id)
        REFERENCES persona_instances(user_id,id,persona_id),
    FOREIGN KEY(template_id,template_version)
        REFERENCES procedure_template_versions(template_id,version),
    FOREIGN KEY(variant_id,variant_revision,user_id,instance_id)
        REFERENCES personalized_procedure_revisions(
            variant_id,revision,user_id,instance_id),
    FOREIGN KEY(task_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX procedure_runs_task_idx
    ON procedure_runs(user_id,instance_id,task_id,created_at DESC);
CREATE INDEX procedure_runs_resume_idx
    ON procedure_runs(user_id,instance_id,status,updated_at DESC);

CREATE TABLE procedure_run_steps (
    run_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    step_id text NOT NULL CHECK(char_length(step_id) BETWEEN 1 AND 40),
    position integer NOT NULL CHECK(position BETWEEN 1 AND 20),
    instruction text NOT NULL CHECK(char_length(instruction) BETWEEN 1 AND 1200),
    success_test text NOT NULL CHECK(char_length(success_test) BETWEEN 1 AND 800),
    executor text NOT NULL CHECK(executor IN ('USER','AGENT')),
    capability text CHECK(capability IS NULL OR char_length(capability) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'NOT_STARTED'
        CHECK(status IN ('NOT_STARTED','INSTRUCTED','ATTEMPTED',
                         'USER_REPORTED_DONE','USER_CONFIRMED',
                         'FAILED','BLOCKED','NEEDS_REVIEW')),
    version bigint NOT NULL DEFAULT 1 CHECK(version>=1),
    last_step_event_id uuid,
    PRIMARY KEY(run_id,step_id),
    UNIQUE(run_id,position),
    UNIQUE(run_id,step_id,user_id,instance_id),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    CHECK ((executor='USER' AND capability IS NULL)
        OR (executor='AGENT' AND capability IS NOT NULL))
);

CREATE TABLE procedure_run_step_events (
    step_event_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    step_id text NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    run_version bigint NOT NULL CHECK(run_version>=2),
    step_version bigint NOT NULL CHECK(step_version>=2),
    kind text NOT NULL CHECK(kind IN (
        'INSTRUCTION_DELIVERED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','CORRECTED')),
    old_status text NOT NULL CHECK(old_status IN (
        'NOT_STARTED','INSTRUCTED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','NEEDS_REVIEW')),
    new_status text NOT NULL CHECK(new_status IN (
        'NOT_STARTED','INSTRUCTED','ATTEMPTED','USER_REPORTED_DONE',
        'USER_CONFIRMED','FAILED','BLOCKED','NEEDS_REVIEW')),
    detail text CHECK(detail IS NULL OR char_length(detail) BETWEEN 10 AND 800),
    corrected_step_event_id uuid,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid,
    response_id uuid,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id,run_version),
    UNIQUE(run_id,step_id,step_version),
    UNIQUE(step_event_id,run_id,step_id),
    CHECK (
        (kind='CORRECTED' AND corrected_step_event_id IS NOT NULL
             AND detail IS NOT NULL AND response_id IS NULL)
        OR
        (kind='INSTRUCTION_DELIVERED' AND response_id IS NOT NULL
             AND corrected_step_event_id IS NULL AND detail IS NULL)
        OR
        (kind NOT IN ('CORRECTED','INSTRUCTION_DELIVERED')
             AND corrected_step_event_id IS NULL AND response_id IS NULL)),
    FOREIGN KEY(run_id,step_id,user_id,instance_id)
        REFERENCES procedure_run_steps(run_id,step_id,user_id,instance_id),
    FOREIGN KEY(corrected_step_event_id,run_id,step_id)
        REFERENCES procedure_run_step_events(step_event_id,run_id,step_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,response_id)
        REFERENCES conversation_responses(conversation_id,response_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX procedure_step_evidence_idx
    ON procedure_run_step_events(run_id,step_id,step_version DESC);
ALTER TABLE procedure_run_steps
    ADD CONSTRAINT procedure_last_step_event_fk
    FOREIGN KEY(last_step_event_id,run_id,step_id)
    REFERENCES procedure_run_step_events(step_event_id,run_id,step_id);

-- An associated dialogue thread remains conversation-owned. Linking does not
-- change focus/resolution and a new session can attach another conversation.
CREATE TABLE procedure_run_thread_links (
    run_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,conversation_id,thread_id),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,thread_id,user_id,instance_id)
        REFERENCES dialogue_threads(conversation_id,thread_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX procedure_run_thread_lookup_idx
    ON procedure_run_thread_links(conversation_id,thread_id);

CREATE FUNCTION reject_procedure_run_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'procedure run evidence history is immutable';
END;
$$;
CREATE TRIGGER immutable_procedure_run_step_events
    BEFORE UPDATE OR DELETE ON procedure_run_step_events
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
CREATE TRIGGER immutable_procedure_run_thread_links
    BEFORE UPDATE OR DELETE ON procedure_run_thread_links
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();

-- The running projection can change status but cannot silently change method
-- pinning, recorded source, ordered steps or success criteria.
CREATE FUNCTION guard_procedure_run_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='procedure_runs' THEN
        IF (OLD.user_id,OLD.instance_id,OLD.persona_id,OLD.template_id,
            OLD.template_version,OLD.variant_id,OLD.variant_revision,
            OLD.task_id,OLD.steps_sha256,OLD.source_conversation_id,
            OLD.source_turn_id,OLD.approval_turn_id,OLD.created_event_id)
           IS DISTINCT FROM
           (NEW.user_id,NEW.instance_id,NEW.persona_id,NEW.template_id,
            NEW.template_version,NEW.variant_id,NEW.variant_revision,
            NEW.task_id,NEW.steps_sha256,NEW.source_conversation_id,
            NEW.source_turn_id,NEW.approval_turn_id,NEW.created_event_id)
        THEN RAISE EXCEPTION 'procedure run pin is immutable'; END IF;
    ELSE
        IF (OLD.run_id,OLD.user_id,OLD.instance_id,OLD.step_id,OLD.position,
            OLD.instruction,OLD.success_test,OLD.executor,OLD.capability)
           IS DISTINCT FROM
           (NEW.run_id,NEW.user_id,NEW.instance_id,NEW.step_id,NEW.position,
            NEW.instruction,NEW.success_test,NEW.executor,NEW.capability)
        THEN RAISE EXCEPTION 'procedure run step definition is immutable'; END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER procedure_run_pin_guard BEFORE UPDATE ON procedure_runs
    FOR EACH ROW EXECUTE FUNCTION guard_procedure_run_projection();
CREATE TRIGGER procedure_step_definition_guard BEFORE UPDATE ON procedure_run_steps
    FOR EACH ROW EXECUTE FUNCTION guard_procedure_run_projection();
