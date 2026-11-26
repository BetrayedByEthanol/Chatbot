-- MVP-05C.3: explicit scoped evidence bindings and directed relations.
-- Bind existing evidence, not a duplicate store of its content. Memory
-- corrections invalidate a binding to an old revision; no implicit relink.
ALTER TABLE memory_episodes ADD CONSTRAINT memory_episode_entity_scope_uq
    UNIQUE(episode_id,user_id,instance_id);

CREATE TABLE memory_entity_evidence_links (
    link_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    memory_id uuid,
    memory_version bigint,
    episode_id uuid,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    event_id uuid NOT NULL,
    CHECK (
        (memory_id IS NOT NULL AND memory_version IS NOT NULL AND memory_version>=1 AND episode_id IS NULL)
        OR (memory_id IS NULL AND memory_version IS NULL AND episode_id IS NOT NULL)
    ),
    FOREIGN KEY(entity_id,user_id,instance_id)
        REFERENCES memory_entities(entity_id,user_id,instance_id),
    FOREIGN KEY(memory_id,user_id,instance_id)
        REFERENCES memory_records(memory_id,user_id,instance_id),
    FOREIGN KEY(memory_id,memory_version) REFERENCES memory_revisions(memory_id,version),
    FOREIGN KEY(episode_id,user_id,instance_id)
        REFERENCES memory_episodes(episode_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE UNIQUE INDEX entity_memory_binding_uq
    ON memory_entity_evidence_links(entity_id,memory_id,memory_version)
    WHERE memory_id IS NOT NULL;
CREATE UNIQUE INDEX entity_episode_binding_uq
    ON memory_entity_evidence_links(entity_id,episode_id) WHERE episode_id IS NOT NULL;
CREATE INDEX entity_evidence_scope_idx
    ON memory_entity_evidence_links(user_id,instance_id,entity_id);
CREATE INDEX entity_evidence_memory_idx ON memory_entity_evidence_links(memory_id,memory_version)
    WHERE memory_id IS NOT NULL;
CREATE INDEX entity_evidence_source_idx
    ON memory_entity_evidence_links(user_id,instance_id,source_turn_id);

-- One directed relation per ordered pair in this bounded vocabulary.
-- No inferred inverse/transitive edges or name-based merging.
CREATE TABLE memory_entity_relationships (
    relationship_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    subject_entity_id uuid NOT NULL,
    object_entity_id uuid NOT NULL,
    relation text NOT NULL CHECK(relation IN ('KNOWS','WORKS_WITH','WORKS_AT','LOCATED_AT')),
    version bigint NOT NULL CHECK(version>=1),
    record_status text NOT NULL CHECK(record_status IN ('ACTIVE','RETRACTED')),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    CHECK(subject_entity_id<>object_entity_id),
    UNIQUE(relationship_id,user_id,instance_id),
    UNIQUE(user_id,instance_id,subject_entity_id,object_entity_id),
    FOREIGN KEY(subject_entity_id,user_id,instance_id)
        REFERENCES memory_entities(entity_id,user_id,instance_id),
    FOREIGN KEY(object_entity_id,user_id,instance_id)
        REFERENCES memory_entities(entity_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX entity_relationship_object_idx
    ON memory_entity_relationships(user_id,instance_id,object_entity_id);
CREATE TABLE memory_entity_relationship_revisions (
    relationship_id uuid NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    action text NOT NULL CHECK(action IN ('REGISTER','CORRECT','RETRACT')),
    relation text NOT NULL CHECK(relation IN ('KNOWS','WORKS_WITH','WORKS_AT','LOCATED_AT')),
    record_status text NOT NULL CHECK(record_status IN ('ACTIVE','RETRACTED')),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL UNIQUE,
    source_turn_at timestamptz NOT NULL,
    source_event_id uuid NOT NULL,
    event_id uuid NOT NULL,
    PRIMARY KEY(relationship_id,version),
    FOREIGN KEY(relationship_id,user_id,instance_id)
        REFERENCES memory_entity_relationships(relationship_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);

CREATE CONSTRAINT TRIGGER historical_recall_entity_link_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_entity_evidence_links
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_entity_relationship_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_entity_relationships
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE INDEX historical_recall_entity_expiry_idx ON memory_entities(user_id,instance_id,expires_at)
    WHERE record_status='ACTIVE' AND privacy_class='NORMAL' AND expires_at IS NOT NULL;
