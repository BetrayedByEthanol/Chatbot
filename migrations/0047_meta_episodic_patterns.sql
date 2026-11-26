-- MVP-05D.2: Memory-owned, versioned hypotheses about multiple episodes.
-- These records are model interpretations, never verified facts or goals.
CREATE TABLE memory_episode_patterns (
    pattern_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    claim_key char(64) NOT NULL,
    fingerprint char(64) NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    topic text NOT NULL CHECK(char_length(topic) BETWEEN 1 AND 80),
    hypothesis text NOT NULL CHECK(char_length(hypothesis) BETWEEN 1 AND 400),
    epistemic_status text NOT NULL DEFAULT 'MODEL_INTERPRETED'
        CHECK(epistemic_status='MODEL_INTERPRETED'),
    model_id text NOT NULL CHECK(char_length(model_id) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(pattern_id,user_id,instance_id),
    UNIQUE(user_id,instance_id,claim_key,version),
    UNIQUE(user_id,instance_id,fingerprint),
    FOREIGN KEY(operation_id,user_id,instance_id)
        REFERENCES cognitive_jobs(operation_id,user_id,instance_id)
);
CREATE INDEX memory_episode_patterns_topic_idx ON memory_episode_patterns
    USING gin(to_tsvector('simple',topic));
CREATE INDEX memory_episode_patterns_scope_idx ON memory_episode_patterns
    (user_id,instance_id,created_at DESC);
CREATE INDEX memory_episode_patterns_operation_idx ON memory_episode_patterns(operation_id);

CREATE TABLE memory_episode_pattern_evidence (
    pattern_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    episode_id uuid NOT NULL,
    role text NOT NULL CHECK(role IN ('SUPPORT','COUNTER')),
    snapshot_hash char(64) NOT NULL,
    PRIMARY KEY(pattern_id,episode_id),
    FOREIGN KEY(pattern_id,user_id,instance_id)
        REFERENCES memory_episode_patterns(pattern_id,user_id,instance_id),
    FOREIGN KEY(episode_id,user_id,instance_id)
        REFERENCES memory_episodes(episode_id,user_id,instance_id)
);
CREATE INDEX memory_episode_pattern_evidence_episode_idx
    ON memory_episode_pattern_evidence(user_id,instance_id,episode_id);

-- Reuse the existing historical read dependency. Pattern commits and
-- evidence corrections cannot silently race a foreground recall response.
CREATE CONSTRAINT TRIGGER historical_recall_pattern_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_episode_patterns
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
CREATE CONSTRAINT TRIGGER historical_recall_pattern_evidence_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_episode_pattern_evidence
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
