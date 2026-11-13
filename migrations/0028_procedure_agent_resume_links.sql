-- P4-05c: explicit new-epoch, new-operation resumption provenance.
-- Link an immutable read-only safe-stop source to a separately approved
-- next-epoch Procedure stage; never mutate/reclaim the old P0 operation.
CREATE TABLE procedure_agent_resume_links (
    operation_id uuid PRIMARY KEY
        REFERENCES procedure_agent_operation_authorizations(operation_id),
    source_operation_id uuid NOT NULL UNIQUE
        REFERENCES operations(operation_id),
    checkpoint_ref text NOT NULL REFERENCES procedure_agent_checkpoints(checkpoint_ref),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    approved_background_epoch bigint NOT NULL CHECK(approved_background_epoch>=1),
    authorization_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(authorization_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE TRIGGER immutable_procedure_agent_resume_links
    BEFORE UPDATE OR DELETE ON procedure_agent_resume_links
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
CREATE INDEX procedure_agent_resume_source_idx
    ON procedure_agent_resume_links(source_operation_id,user_id,instance_id);
