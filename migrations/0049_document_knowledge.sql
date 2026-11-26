-- MVP-05D.4: private supplied-text knowledge, not Procedure Manager state.
CREATE TABLE memory_knowledge_documents (
    document_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    current_version bigint NOT NULL CHECK(current_version>=1),
    record_status text NOT NULL DEFAULT 'ACTIVE' CHECK(record_status IN ('ACTIVE','WITHDRAWN')),
    privacy_class text NOT NULL CHECK(privacy_class IN ('NORMAL','RESTRICTED')),
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(document_id,user_id,instance_id),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id)
);
CREATE INDEX memory_knowledge_documents_scope_idx
    ON memory_knowledge_documents(user_id,instance_id,current_version)
    WHERE record_status='ACTIVE' AND privacy_class='NORMAL';
CREATE TABLE memory_knowledge_versions (
    document_id uuid NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 160),
    source_kind text NOT NULL CHECK(source_kind IN ('MANUAL','DOCUMENTATION','RESEARCH','USER_REFERENCE')),
    source_locator text NOT NULL CHECK(char_length(source_locator) BETWEEN 1 AND 500),
    source_revision text CHECK(source_revision IS NULL OR char_length(source_revision) BETWEEN 1 AND 120),
    content text NOT NULL CHECK(char_length(content) BETWEEN 1 AND 24000),
    content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    epistemic_status text NOT NULL DEFAULT 'SUPPLIED_SOURCE' CHECK(epistemic_status='SUPPLIED_SOURCE'),
    privacy_class text NOT NULL CHECK(privacy_class IN ('NORMAL','RESTRICTED')),
    expires_at timestamptz,
    source_conversation_id uuid NOT NULL,
    evidence_turn_id uuid NOT NULL,
    approval_turn_id uuid NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(document_id,version),
    UNIQUE(document_id,version,user_id,instance_id),
    FOREIGN KEY(document_id,user_id,instance_id)
        REFERENCES memory_knowledge_documents(document_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,evidence_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_conversation_id,approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE TABLE memory_knowledge_passages (
    document_id uuid NOT NULL,
    version bigint NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    position integer NOT NULL CHECK(position>=1),
    start_offset integer NOT NULL CHECK(start_offset>=0),
    end_offset integer NOT NULL CHECK(end_offset>start_offset),
    passage_text text NOT NULL CHECK(char_length(passage_text) BETWEEN 1 AND 600),
    CHECK(end_offset-start_offset=char_length(passage_text)),
    PRIMARY KEY(document_id,version,position),
    FOREIGN KEY(document_id,version,user_id,instance_id)
        REFERENCES memory_knowledge_versions(document_id,version,user_id,instance_id)
);
CREATE INDEX memory_knowledge_passages_search_idx ON memory_knowledge_passages
    USING gin(to_tsvector('simple',passage_text));
CREATE TABLE memory_knowledge_procedure_refs (
    document_id uuid NOT NULL,
    version bigint NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    variant_id uuid NOT NULL,
    procedure_revision integer NOT NULL,
    PRIMARY KEY(document_id,version,variant_id),
    FOREIGN KEY(document_id,version,user_id,instance_id)
        REFERENCES memory_knowledge_versions(document_id,version,user_id,instance_id),
    FOREIGN KEY(variant_id,user_id,instance_id)
        REFERENCES personalized_procedures(variant_id,user_id,instance_id),
    FOREIGN KEY(variant_id,procedure_revision)
        REFERENCES personalized_procedure_revisions(variant_id,revision)
);
CREATE INDEX memory_knowledge_procedure_refs_scope_idx
    ON memory_knowledge_procedure_refs(user_id,instance_id,variant_id,procedure_revision);
-- Direct excerpt replies remain transcript records, not reusable model evidence.
ALTER TABLE conversation_responses ADD COLUMN knowledge_excerpt boolean NOT NULL DEFAULT false;
CREATE CONSTRAINT TRIGGER historical_recall_knowledge_document_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_knowledge_documents
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_knowledge_version_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_knowledge_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_knowledge_passage_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_knowledge_passages
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_knowledge_reference_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_knowledge_procedure_refs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
