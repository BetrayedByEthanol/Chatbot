-- Phase 3 / P3-03: curated persona-global templates and private pinned variants.
-- No automated promotion, mutable definitions, procedure runs or tool authority.
-- Publishing grants are provisioned out-of-band by an authorized platform operator.
CREATE TABLE procedure_publish_grants (
    user_id uuid NOT NULL REFERENCES users(id),
    persona_id uuid NOT NULL REFERENCES persona_definitions(id),
    granted_by text NOT NULL CHECK (char_length(granted_by) BETWEEN 1 AND 160),
    active boolean NOT NULL DEFAULT true,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id,persona_id)
);

CREATE TABLE procedure_templates (
    template_id uuid PRIMARY KEY,
    persona_id uuid NOT NULL REFERENCES persona_definitions(id),
    current_version integer NOT NULL CHECK(current_version>=1),
    approved boolean NOT NULL DEFAULT true CHECK(approved),
    publisher_user_id uuid NOT NULL REFERENCES users(id),
    created_event_id uuid NOT NULL REFERENCES events(event_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(template_id,persona_id)
);

CREATE TABLE procedure_template_versions (
    template_id uuid NOT NULL REFERENCES procedure_templates(template_id),
    persona_id uuid NOT NULL,
    version integer NOT NULL CHECK(version>=1),
    schema_version integer NOT NULL DEFAULT 1 CHECK(schema_version=1),
    definition jsonb NOT NULL CHECK(jsonb_typeof(definition)='object'),
    definition_sha256 text NOT NULL CHECK(definition_sha256 ~ '^[0-9a-f]{64}$'),
    review_note text NOT NULL CHECK(char_length(review_note) BETWEEN 10 AND 800),
    source_user_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    published_event_id uuid NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(template_id,version),
    FOREIGN KEY(template_id,persona_id)
        REFERENCES procedure_templates(template_id,persona_id),
    FOREIGN KEY(source_user_id,source_instance_id)
        REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(source_conversation_id,source_turn_id,source_user_id,source_instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,source_user_id,source_instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(published_event_id,source_user_id,source_instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

-- A personal overlay can never acquire persona-global ownership. Its template
-- version and parameters are pinned in append-only revision records.
ALTER TABLE persona_instances
    ADD CONSTRAINT persona_instances_definition_scope_uq
    UNIQUE(user_id,id,persona_id);

CREATE TABLE personalized_procedures (
    variant_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    template_id uuid NOT NULL,
    goal_id uuid,
    strategy_id uuid,
    current_revision integer NOT NULL CHECK(current_revision>=1),
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((goal_id IS NULL)=(strategy_id IS NULL)),
    UNIQUE(variant_id,user_id,instance_id),
    UNIQUE(variant_id,template_id,user_id,instance_id),
    FOREIGN KEY(user_id,instance_id,persona_id)
        REFERENCES persona_instances(user_id,id,persona_id),
    FOREIGN KEY(template_id,persona_id)
        REFERENCES procedure_templates(template_id,persona_id),
    FOREIGN KEY(goal_id,user_id,instance_id)
        REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY(strategy_id,goal_id,user_id,instance_id)
        REFERENCES goal_strategies(strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX personalized_procedures_scope_idx
    ON personalized_procedures(user_id,instance_id,created_at DESC);

CREATE TABLE personalized_procedure_revisions (
    variant_id uuid NOT NULL,
    template_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    revision integer NOT NULL CHECK(revision>=1),
    template_version integer NOT NULL CHECK(template_version>=1),
    parameters jsonb NOT NULL CHECK(jsonb_typeof(parameters)='object'),
    step_text_overrides jsonb NOT NULL CHECK(jsonb_typeof(step_text_overrides)='object'),
    adaptation_reason text,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL CHECK(char_length(source_quote) BETWEEN 1 AND 300),
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(variant_id,revision),
    FOREIGN KEY(variant_id,template_id,user_id,instance_id)
        REFERENCES personalized_procedures(variant_id,template_id,user_id,instance_id),
    FOREIGN KEY(template_id,template_version)
        REFERENCES procedure_template_versions(template_id,version),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

-- Database-level immutability protects historical versions even when a caller
-- bypasses the domain writer. Only new version/revision rows may be inserted.
CREATE FUNCTION reject_procedure_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'procedure version/revision history is immutable';
END;
$$;
CREATE TRIGGER immutable_procedure_template_versions
    BEFORE UPDATE OR DELETE ON procedure_template_versions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
CREATE TRIGGER immutable_personalized_procedure_revisions
    BEFORE UPDATE OR DELETE ON personalized_procedure_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
