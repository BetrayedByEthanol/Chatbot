-- P3-05: Procedure-owned observations and private proposals.
-- User-reported success is NEVER independent verification of a Task/Goal.
-- Historical evaluations, proposals, and decisions are append-only.

CREATE TABLE procedure_run_evaluations (
    evaluation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    evaluation_revision integer NOT NULL CHECK(evaluation_revision>=1),
    run_version bigint NOT NULL CHECK(run_version>=1),
    supersedes_evaluation_id uuid,
    outcome text NOT NULL CHECK(outcome IN (
        'UNKNOWN','INCOMPLETE','USER_REPORTED_SUCCESS','USER_REPORTED_FAILURE')),
    failure_cause text CHECK(failure_cause IN (
        'UNDETERMINED','INCORRECT_INPUT','ENVIRONMENT',
        'INCOMPLETE_USER_ACTION','METHOD_MISMATCH','INVALID_SUCCESS_CRITERIA')),
    confidence double precision NOT NULL CHECK(confidence>=0 AND confidence<=1),
    rationale text NOT NULL CHECK(char_length(rationale) BETWEEN 10 AND 800),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((outcome='USER_REPORTED_FAILURE')=(failure_cause IS NOT NULL)),
    UNIQUE(run_id,evaluation_revision),
    UNIQUE(evaluation_id,run_id,user_id,instance_id),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    FOREIGN KEY(supersedes_evaluation_id,run_id,user_id,instance_id)
        REFERENCES procedure_run_evaluations(evaluation_id,run_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX procedure_evaluations_latest
    ON procedure_run_evaluations(run_id,evaluation_revision DESC);

-- Preserve exact immutable step events supporting each assessment.
CREATE TABLE procedure_evaluation_step_evidence (
    evaluation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    step_id text NOT NULL,
    step_event_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    PRIMARY KEY(evaluation_id,step_event_id),
    FOREIGN KEY(evaluation_id,run_id,user_id,instance_id)
        REFERENCES procedure_run_evaluations(evaluation_id,run_id,user_id,instance_id),
    FOREIGN KEY(step_event_id,run_id,step_id)
        REFERENCES procedure_run_step_events(step_event_id,run_id,step_id),
    FOREIGN KEY(run_id,step_id,user_id,instance_id)
        REFERENCES procedure_run_steps(run_id,step_id,user_id,instance_id)
);

CREATE TABLE procedure_adaptation_proposals (
    proposal_id uuid PRIMARY KEY,
    evaluation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    variant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    expected_revision integer NOT NULL CHECK(expected_revision>=1),
    template_version integer NOT NULL CHECK(template_version>=1),
    policy text NOT NULL CHECK(policy IN ('PARAMETERIZED','ADAPTIVE')),
    parameters jsonb NOT NULL CHECK(jsonb_typeof(parameters)='object'),
    step_text_overrides jsonb NOT NULL CHECK(jsonb_typeof(step_text_overrides)='object'),
    reason text NOT NULL CHECK(char_length(reason) BETWEEN 10 AND 800),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(proposal_id,variant_id,user_id,instance_id),
    FOREIGN KEY(evaluation_id,run_id,user_id,instance_id)
        REFERENCES procedure_run_evaluations(evaluation_id,run_id,user_id,instance_id),
    FOREIGN KEY(run_id,user_id,instance_id)
        REFERENCES procedure_runs(run_id,user_id,instance_id),
    FOREIGN KEY(variant_id,user_id,instance_id)
        REFERENCES personalized_procedures(variant_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX procedure_proposals_variant_idx
    ON procedure_adaptation_proposals(variant_id,created_at DESC);

CREATE TABLE procedure_adaptation_decisions (
    proposal_id uuid PRIMARY KEY,
    variant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    decision text NOT NULL CHECK(decision IN ('APPLY','REJECT')),
    applied_revision integer CHECK(applied_revision>=2),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((decision='APPLY')=(applied_revision IS NOT NULL)),
    FOREIGN KEY(proposal_id,variant_id,user_id,instance_id)
        REFERENCES procedure_adaptation_proposals(
            proposal_id,variant_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

CREATE TRIGGER immutable_procedure_run_evaluations
    BEFORE UPDATE OR DELETE ON procedure_run_evaluations
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
CREATE TRIGGER immutable_procedure_evaluation_step_evidence
    BEFORE UPDATE OR DELETE ON procedure_evaluation_step_evidence
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
CREATE TRIGGER immutable_procedure_adaptation_proposals
    BEFORE UPDATE OR DELETE ON procedure_adaptation_proposals
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
CREATE TRIGGER immutable_procedure_adaptation_decisions
    BEFORE UPDATE OR DELETE ON procedure_adaptation_decisions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_run_history_mutation();
