-- P2-F1.3: immutable forecast, exact ingress bindings and at-most-once resolution.
-- No speculative inference, external effect or generic evidence graph.
ALTER TABLE cognitive_expectation_revisions
    ADD UNIQUE(revision_id,user_id,instance_id);

CREATE TABLE cognitive_predictions (
    prediction_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    control_version bigint NOT NULL CHECK(control_version>=1),
    expectation_revision_id uuid NOT NULL,
    expectation_version bigint NOT NULL CHECK(expectation_version>=1),
    operation_id uuid NOT NULL,
    commitment_turn_id uuid NOT NULL,
    context_key text NOT NULL CHECK(char_length(context_key) BETWEEN 1 AND 64),
    target_occasion date NOT NULL,
    expected_completion boolean NOT NULL,
    source_turn_ids uuid[] NOT NULL CHECK(cardinality(source_turn_ids) BETWEEN 2 AND 128
        AND array_position(source_turn_ids,NULL) IS NULL),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK(expires_at>created_at),
    UNIQUE(prediction_id,user_id,instance_id),
    UNIQUE(user_id,instance_id,reset_generation,context_key,target_occasion),
    UNIQUE(commitment_turn_id),
    FOREIGN KEY(expectation_revision_id,user_id,instance_id)
        REFERENCES cognitive_expectation_revisions(revision_id,user_id,instance_id),
    FOREIGN KEY(operation_id,user_id,instance_id)
        REFERENCES cognitive_jobs(operation_id,user_id,instance_id),
    FOREIGN KEY(commitment_turn_id) REFERENCES predictive_input_admissions(turn_id)
);
CREATE TRIGGER immutable_cognitive_prediction
    BEFORE UPDATE OR DELETE ON cognitive_predictions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();

-- Bind only exact supported reports to rows already visible at ingress.
-- A NULL pin is intentionally never filled by a delayed worker.
ALTER TABLE predictive_input_admissions
    ADD COLUMN reported_context text,
    ADD COLUMN reported_occasion date,
    ADD COLUMN reported_completion boolean,
    ADD COLUMN reported_at timestamptz,
    ADD COLUMN comparison_expectation_id uuid,
    ADD COLUMN prediction_id uuid,
    ADD FOREIGN KEY(comparison_expectation_id,user_id,instance_id)
        REFERENCES cognitive_expectation_revisions(revision_id,user_id,instance_id),
    ADD FOREIGN KEY(prediction_id,user_id,instance_id)
        REFERENCES cognitive_predictions(prediction_id,user_id,instance_id),
    ADD CHECK(
        (reported_context IS NULL AND reported_occasion IS NULL AND reported_completion IS NULL
         AND reported_at IS NULL AND comparison_expectation_id IS NULL AND prediction_id IS NULL)
        OR (reported_context IS NOT NULL AND char_length(reported_context) BETWEEN 1 AND 64
            AND reported_occasion IS NOT NULL AND reported_completion IS NOT NULL
            AND reported_at IS NOT NULL)
    );
CREATE INDEX predictive_report_occasion_idx ON predictive_input_admissions(
    user_id,instance_id,reset_generation,reported_context,reported_occasion,reported_at,turn_id
) WHERE reported_context IS NOT NULL;
CREATE INDEX predictive_report_prediction_idx ON predictive_input_admissions(
    prediction_id,reported_at,turn_id
) WHERE prediction_id IS NOT NULL;

CREATE TABLE cognitive_prediction_resolutions (
    prediction_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    control_version bigint NOT NULL CHECK(control_version>=1),
    outcome_turn_ids uuid[] NOT NULL CHECK(cardinality(outcome_turn_ids) BETWEEN 1 AND 16
        AND array_position(outcome_turn_ids,NULL) IS NULL),
    resolution text NOT NULL CHECK(resolution IN ('HIT','MISS','INCONCLUSIVE')),
    comparison text NOT NULL,
    resolved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((resolution='HIT' AND comparison='EXPECTED')
       OR (resolution='MISS' AND comparison='UNEXPECTED')
       OR (resolution='INCONCLUSIVE' AND comparison='CONTRADICTORY')),
    FOREIGN KEY(prediction_id,user_id,instance_id)
        REFERENCES cognitive_predictions(prediction_id,user_id,instance_id),
    FOREIGN KEY(operation_id,user_id,instance_id)
        REFERENCES cognitive_jobs(operation_id,user_id,instance_id)
);
CREATE TRIGGER immutable_prediction_resolution
    BEFORE UPDATE OR DELETE ON cognitive_prediction_resolutions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();

-- Durable bounded comparison candidates, not an emotion or reappraisal action.
-- Repeated same-occasion/polarity evidence cannot queue the same mismatch twice.
CREATE TABLE cognitive_expectation_comparisons (
    comparison_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    expectation_revision_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    control_version bigint NOT NULL CHECK(control_version>=1),
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    outcome_turn_id uuid NOT NULL UNIQUE REFERENCES predictive_input_admissions(turn_id),
    occasion date NOT NULL,
    completed boolean NOT NULL,
    classification text NOT NULL CHECK(classification IN ('EXPECTED','UNEXPECTED','CONTRADICTORY')),
    source_turn_ids uuid[] NOT NULL CHECK(cardinality(source_turn_ids) BETWEEN 2 AND 128
        AND array_position(source_turn_ids,NULL) IS NULL),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(expectation_revision_id,occasion,completed),
    FOREIGN KEY(expectation_revision_id,user_id,instance_id)
        REFERENCES cognitive_expectation_revisions(revision_id,user_id,instance_id),
    FOREIGN KEY(operation_id,user_id,instance_id)
        REFERENCES cognitive_jobs(operation_id,user_id,instance_id)
);
CREATE INDEX cognitive_prediction_scope_idx ON cognitive_predictions(user_id,instance_id,created_at DESC);
CREATE INDEX cognitive_comparison_scope_idx ON cognitive_expectation_comparisons(user_id,instance_id,created_at DESC);
CREATE TRIGGER immutable_expectation_comparison
    BEFORE UPDATE OR DELETE ON cognitive_expectation_comparisons
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
