-- MVP-05D.3: per-revision constituent spans and lower-trust associations.
-- memory_records and memory_revisions remain the only definition authority.
CREATE TABLE memory_meaning_components (
    memory_id uuid NOT NULL,
    version bigint NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    position smallint NOT NULL CHECK(position BETWEEN 1 AND 5),
    component_text text NOT NULL CHECK(char_length(component_text) BETWEEN 1 AND 100),
    PRIMARY KEY(memory_id,version,position),
    UNIQUE(memory_id,version,position,user_id,instance_id),
    FOREIGN KEY(memory_id,version) REFERENCES memory_revisions(memory_id,version),
    FOREIGN KEY(memory_id,user_id,instance_id)
        REFERENCES memory_records(memory_id,user_id,instance_id)
);
CREATE TABLE memory_meaning_associations (
    memory_id uuid NOT NULL,
    version bigint NOT NULL,
    position smallint NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    concept_key text NOT NULL CHECK(char_length(concept_key) BETWEEN 1 AND 64),
    epistemic_status text NOT NULL DEFAULT 'RULE_INFERRED'
        CHECK(epistemic_status='RULE_INFERRED'),
    rule_version text NOT NULL CHECK(char_length(rule_version) BETWEEN 1 AND 64),
    PRIMARY KEY(memory_id,version,position,concept_key),
    FOREIGN KEY(memory_id,version,position,user_id,instance_id)
        REFERENCES memory_meaning_components(memory_id,version,position,user_id,instance_id)
);
CREATE CONSTRAINT TRIGGER historical_recall_meaning_component_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_meaning_components
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_meaning_association_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_meaning_associations
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
