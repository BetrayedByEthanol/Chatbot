-- MVP-05C.1: scoped, explicitly registered identities and user relations.
-- Names are NOT identity keys. Equal names/aliases may refer to different
-- entities. No automatic merging or historical name extraction.
CREATE TABLE memory_entities (
    entity_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    entity_kind text NOT NULL CHECK (entity_kind IN ('PERSON','PLACE','OBJECT')),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
    aliases text[] NOT NULL DEFAULT '{}' CHECK (cardinality(aliases)<=8),
    name_keys text[] NOT NULL CHECK (
        cardinality(name_keys)=cardinality(aliases)+1
    ),
    relation text NOT NULL CHECK (
        (entity_kind='PERSON' AND relation IN ('FRIEND','COLLEAGUE','SIBLING'))
        OR (entity_kind='PLACE' AND relation='FAVORITE_PLACE')
        OR (entity_kind='OBJECT' AND relation IN ('BICYCLE','GUITAR','CAMERA','BOOK','OBJECT'))
    ),
    version bigint NOT NULL DEFAULT 1 CHECK (version>=1),
    record_status text NOT NULL DEFAULT 'ACTIVE' CHECK (record_status IN ('ACTIVE','RETRACTED')),
    privacy_class text NOT NULL DEFAULT 'NORMAL' CHECK (privacy_class IN ('NORMAL','SENSITIVE')),
    expires_at timestamptz,
    source_turn_at timestamptz NOT NULL,
    relation_source_conversation_id uuid NOT NULL,
    relation_source_turn_id uuid NOT NULL,
    UNIQUE(entity_id,user_id,instance_id),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(relation_source_conversation_id,relation_source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id)
);
CREATE INDEX memory_entities_scope_idx ON memory_entities(user_id,instance_id)
    WHERE record_status='ACTIVE' AND privacy_class='NORMAL';
CREATE INDEX memory_entities_names_idx ON memory_entities USING gin(name_keys)
    WHERE record_status='ACTIVE' AND privacy_class='NORMAL';

CREATE TABLE memory_entity_revisions (
    entity_id uuid NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    action text NOT NULL CHECK(action IN ('REGISTER','CORRECT_RELATION','ADD_ALIAS','RETRACT')),
    name text NOT NULL,
    aliases text[] NOT NULL,
    relation text NOT NULL,
    record_status text NOT NULL CHECK(record_status IN ('ACTIVE','RETRACTED')),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL UNIQUE,
    source_event_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    event_id uuid NOT NULL,
    PRIMARY KEY(entity_id,version),
    FOREIGN KEY(entity_id,user_id,instance_id)
        REFERENCES memory_entities(entity_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);

-- Reuse the existing scoped historical revision for atomic visibility.
-- A canonical reader takes the shared revision lock and reads current
-- committed identities without locking entity rows (same lock order as 05B).
CREATE CONSTRAINT TRIGGER historical_recall_entity_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_entities
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();

-- The preceding delivered response's entity binding is provenance, not
-- an independent memory or authority. Index the existing event journal.
CREATE INDEX conversation_entity_grounding_response_idx
    ON events(user_id,instance_id,(payload->>'response_id'))
    WHERE event_type='conversation.response.entity_grounded.v1';
