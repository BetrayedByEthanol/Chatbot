-- MVP-07B2: every reflection and intention from a pass shares its model input
-- dependencies, not only the turn selected as a citation. Legacy NULL is unknown.
ALTER TABLE cognitive_jobs
    ADD COLUMN source_turn_ids uuid[],
    ADD COLUMN source_episode_ids uuid[],
    ADD COLUMN source_episode_hashes text[],
    ADD COLUMN source_digest char(64),
    ADD CHECK (
        (source_turn_ids IS NULL AND source_episode_ids IS NULL
         AND source_episode_hashes IS NULL AND source_digest IS NULL)
        OR (
            committed_at IS NOT NULL AND source_turn_ids IS NOT NULL
            AND source_episode_ids IS NOT NULL AND source_episode_hashes IS NOT NULL
            AND source_digest IS NOT NULL
            AND cardinality(source_turn_ids)<=16
            AND cardinality(source_episode_ids)<=6
            AND cardinality(source_episode_ids)=cardinality(source_episode_hashes)
            AND array_position(source_turn_ids,NULL) IS NULL
            AND array_position(source_episode_ids,NULL) IS NULL
            AND array_position(source_episode_hashes,NULL) IS NULL
        )
    );

-- Persisted dependency metadata is immutable after a committed pass. Do not
-- fabricate a source set for legacy jobs. The owner can still remove rows
-- through a future authorized lifecycle contract.
CREATE FUNCTION protect_cognitive_source_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.committed_at IS NOT NULL AND (
        NEW.source_turn_ids,NEW.source_episode_ids,NEW.source_episode_hashes,NEW.source_digest
    ) IS DISTINCT FROM (
        OLD.source_turn_ids,OLD.source_episode_ids,OLD.source_episode_hashes,OLD.source_digest
    ) THEN
        RAISE EXCEPTION 'committed cognitive source lineage is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER protect_cognitive_source_lineage
    BEFORE UPDATE ON cognitive_jobs FOR EACH ROW
    EXECUTE FUNCTION protect_cognitive_source_lineage();

-- A review/retraction can invalidate an already-prepared foreground context.
-- New independent proposals do not invalidate previous response dependencies.
CREATE CONSTRAINT TRIGGER historical_recall_reflection_changed
    AFTER UPDATE OR DELETE ON cognitive_reflections
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
