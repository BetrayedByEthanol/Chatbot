-- Cognitive-owned, immutable contextual expectation revisions.
-- No global reliability score, forecasts, or authority to change other owners.
CREATE TABLE cognitive_expectation_revisions (
    revision_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    context_key text NOT NULL CHECK(char_length(context_key) BETWEEN 1 AND 64),
    version bigint NOT NULL CHECK(version>=1),
    operation_id uuid,
    control_version bigint NOT NULL CHECK(control_version>=1),
    disposition text NOT NULL CHECK(disposition IN ('HYPOTHESIS','REVIEW_REQUIRED','WITHDRAWN')),
    maturity text NOT NULL CHECK(maturity IN ('EARLY','TENTATIVE','CONFLICTED','UNAVAILABLE')),
    direction text NOT NULL CHECK(direction IN ('COMPLETION','NON_COMPLETION','MIXED','UNKNOWN')),
    completion_occasions integer NOT NULL CHECK(completion_occasions>=0),
    non_completion_occasions integer NOT NULL CHECK(non_completion_occasions>=0),
    conflicting_occasions integer NOT NULL CHECK(conflicting_occasions>=0),
    source_turn_ids uuid[] NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_event_id uuid,
    UNIQUE(user_id,instance_id,reset_generation,context_key,version),
    UNIQUE(operation_id,context_key),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(operation_id,user_id,instance_id) REFERENCES cognitive_jobs(operation_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    CHECK(array_position(source_turn_ids,NULL) IS NULL),
    CHECK((disposition='WITHDRAWN' AND operation_id IS NULL AND source_event_id IS NOT NULL
           AND cardinality(source_turn_ids)=0 AND expires_at IS NULL
           AND maturity='UNAVAILABLE' AND direction='UNKNOWN'
           AND completion_occasions=0 AND non_completion_occasions=0 AND conflicting_occasions=0)
       OR (disposition<>'WITHDRAWN' AND operation_id IS NOT NULL AND source_event_id IS NULL
           AND cardinality(source_turn_ids) BETWEEN 1 AND 128 AND expires_at IS NOT NULL))
);
CREATE TRIGGER immutable_expectation_revision
    BEFORE UPDATE OR DELETE ON cognitive_expectation_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
