-- P3-02 correction hardening (post-P3-03): never rewrite user evidence.
-- A corrected task has an explicit review state; its prior reports and
-- confirmations remain auditable and later valid work can be reconfirmed.

ALTER TABLE goal_tasks DROP CONSTRAINT goal_tasks_status_check;
ALTER TABLE goal_tasks ADD CONSTRAINT goal_tasks_status_check
    CHECK(status IN ('PLANNED','USER_REPORTED_DONE','USER_CONFIRMED',
                     'NEEDS_REVIEW','CANCELLED'));

-- Existing one-confirmation-per-task uniqueness prevents legitimate
-- correction and later reconfirmation. A report may still be confirmed once.
ALTER TABLE task_user_confirmations
    DROP CONSTRAINT task_user_confirmations_task_id_key;
ALTER TABLE task_user_confirmations
    ADD CONSTRAINT task_user_confirmations_scoped_id_uq
    UNIQUE(confirmation_id,task_id,goal_id,user_id,instance_id);
CREATE UNIQUE INDEX task_user_confirmations_task_version_uq
    ON task_user_confirmations(task_id,task_version);
ALTER TABLE task_user_confirmations
    ADD CONSTRAINT task_user_confirmations_scoped_task_id_uq
    UNIQUE(confirmation_id,task_id,user_id,instance_id);

-- One immutable snapshot edge for each direct dependency/RUN_AFTER relation
-- that justified a user confirmation. If a predecessor is later corrected
-- and reconfirmed with a different confirmation ID, descendants stay invalid
-- until independently reassessed and reconfirmed.
CREATE TABLE task_confirmation_prerequisites (
    confirmation_id uuid NOT NULL,
    task_id uuid NOT NULL,
    prerequisite_task_id uuid NOT NULL,
    prerequisite_confirmation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    PRIMARY KEY(confirmation_id,prerequisite_task_id),
    CHECK(task_id<>prerequisite_task_id),
    FOREIGN KEY(confirmation_id,task_id,user_id,instance_id)
        REFERENCES task_user_confirmations(
            confirmation_id,task_id,user_id,instance_id),
    FOREIGN KEY(prerequisite_confirmation_id,prerequisite_task_id,user_id,instance_id)
        REFERENCES task_user_confirmations(
            confirmation_id,task_id,user_id,instance_id)
);
CREATE INDEX task_confirmation_prerequisites_target_idx
    ON task_confirmation_prerequisites(prerequisite_task_id);

CREATE TABLE task_outcome_corrections (
    correction_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    task_version bigint NOT NULL CHECK(task_version>=2),
    superseded_report_id uuid UNIQUE,
    superseded_confirmation_id uuid UNIQUE,
    old_status text NOT NULL CHECK (
        old_status IN ('PLANNED','USER_REPORTED_DONE','USER_CONFIRMED')),
    new_status text NOT NULL DEFAULT 'NEEDS_REVIEW'
        CHECK(new_status='NEEDS_REVIEW'),
    reason text NOT NULL CHECK(char_length(reason) BETWEEN 10 AND 800),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(task_id,task_version),
    CHECK ((superseded_report_id IS NOT NULL)
        <> (superseded_confirmation_id IS NOT NULL)),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(superseded_report_id,task_id,goal_id,user_id,instance_id)
        REFERENCES task_outcome_reports(report_id,task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(superseded_confirmation_id,task_id,goal_id,user_id,instance_id)
        REFERENCES task_user_confirmations(
            confirmation_id,task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX task_corrections_recent_idx
    ON task_outcome_corrections(task_id,task_version DESC);

-- Historic evidence is never rewritten. A correction is a new versioned fact,
-- not an UPDATE to a previous confirmation, report or correction.
CREATE FUNCTION reject_task_evidence_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'task evidence history is immutable';
END;
$$;
CREATE TRIGGER immutable_task_outcome_reports
    BEFORE UPDATE OR DELETE ON task_outcome_reports
    FOR EACH ROW EXECUTE FUNCTION reject_task_evidence_mutation();
CREATE TRIGGER immutable_task_user_confirmations
    BEFORE UPDATE OR DELETE ON task_user_confirmations
    FOR EACH ROW EXECUTE FUNCTION reject_task_evidence_mutation();
CREATE TRIGGER immutable_task_outcome_corrections
    BEFORE UPDATE OR DELETE ON task_outcome_corrections
    FOR EACH ROW EXECUTE FUNCTION reject_task_evidence_mutation();
CREATE TRIGGER immutable_task_confirmation_prerequisites
    BEFORE UPDATE OR DELETE ON task_confirmation_prerequisites
    FOR EACH ROW EXECUTE FUNCTION reject_task_evidence_mutation();
