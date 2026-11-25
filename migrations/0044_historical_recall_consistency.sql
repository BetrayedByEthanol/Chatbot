-- MVP-05B.3: source-linked explicit reports, not admitted Memory records.
-- Existing inputs are not reinterpreted during migration.
CREATE TABLE explicit_memory_reports (
    source_turn_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    memory_kind text NOT NULL CHECK (
        memory_kind IN ('FACT','PREFERENCE','PERSONAL_MEANING')
    ),
    subject_key text NOT NULL CHECK (subject_key='user'),
    predicate_key text NOT NULL CHECK (char_length(predicate_key) BETWEEN 1 AND 64),
    value_text text NOT NULL CHECK (char_length(value_text) BETWEEN 1 AND 100),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX explicit_memory_reports_slot_idx ON explicit_memory_reports
    (user_id,instance_id,memory_kind,subject_key,predicate_key);
CREATE INDEX historical_recall_expiry_idx ON memory_records
    (user_id,instance_id,expires_at)
    WHERE record_status='ACTIVE' AND privacy_class='NORMAL' AND expires_at IS NOT NULL;

-- Conservative per-persona read dependency, not a replacement for record
-- versions or a lock held across inference.
CREATE TABLE historical_recall_revisions (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    PRIMARY KEY (user_id,instance_id),
    FOREIGN KEY (user_id,instance_id) REFERENCES persona_instances(user_id,id)
        ON DELETE CASCADE
);
INSERT INTO historical_recall_revisions(user_id,instance_id)
    SELECT user_id,id FROM persona_instances;

CREATE FUNCTION initialize_historical_recall_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO historical_recall_revisions(user_id,instance_id)
        VALUES (NEW.user_id,NEW.id);
    RETURN NEW;
END;
$$;
CREATE TRIGGER initialize_historical_recall_revision
    AFTER INSERT ON persona_instances FOR EACH ROW
    EXECUTE FUNCTION initialize_historical_recall_revision();

-- Deferred triggers acquire the revision row only at commit, after
-- ordinary owner row locks and event writes. A response commit takes
-- its shared revision lock after conversation/procedure locks, and does
-- not acquire Memory record/candidate locks afterwards.
CREATE FUNCTION bump_historical_recall_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE historical_recall_revisions SET revision=revision+1
          WHERE user_id=OLD.user_id AND instance_id=OLD.instance_id;
    ELSE
        UPDATE historical_recall_revisions SET revision=revision+1
          WHERE user_id=NEW.user_id AND instance_id=NEW.instance_id;
        IF TG_OP='UPDATE' THEN
            IF (OLD.user_id,OLD.instance_id) IS DISTINCT FROM (NEW.user_id,NEW.instance_id) THEN
                UPDATE historical_recall_revisions SET revision=revision+1
                  WHERE user_id=OLD.user_id AND instance_id=OLD.instance_id;
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER historical_recall_memory_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_records
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_candidate_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_extraction_candidates
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_report_changed
    AFTER INSERT OR UPDATE OR DELETE ON explicit_memory_reports
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_episode_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_episodes
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_summary_changed
    AFTER INSERT OR UPDATE OR DELETE ON conversation_summaries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
