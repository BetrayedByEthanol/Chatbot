-- MVP-07B2.8: owner projections retain the Memory eligibility watermark.
-- Legacy projections are unbound until reappraised; do not guess provenance.
ALTER TABLE persona_emotion_state ADD COLUMN evidence_revision bigint;
ALTER TABLE persona_emotion_state ADD COLUMN evidence_next_expiry timestamptz;
ALTER TABLE relationship_state ADD COLUMN evidence_revision bigint;
ALTER TABLE relationship_state ADD COLUMN evidence_next_expiry timestamptz;

CREATE INDEX emotion_applied_scope_idx ON emotion_appraisals
    (user_id,instance_id,source_turn_at,appraisal_id) WHERE applied;
CREATE INDEX relationship_feedback_scope_idx ON relationship_observations
    (user_id,instance_id,source_turn_at) WHERE feedback<>'NONE';

-- Effective preference state never falls back to a superseded revision.
-- The latest stored revision remains the optimistic concurrency authority.
CREATE VIEW current_relationship_preferences AS
    SELECT p.* FROM relationship_preferences p
    WHERE dialogue_turn_current(p.user_id,p.instance_id,
                                p.source_conversation_id,p.source_turn_id);

-- Presentation context can use any scoped preference, including a topic
-- boundary. Invalidate broad readers on an owner preference change.
CREATE CONSTRAINT TRIGGER historical_recall_relationship_preference_changed
    AFTER INSERT OR UPDATE OR DELETE ON relationship_preferences
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();

CREATE OR REPLACE FUNCTION response_lineage_current(rid uuid,uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM conversation_response_lineage l
        LEFT JOIN historical_recall_revisions h
          ON h.user_id=l.user_id AND h.instance_id=l.instance_id
        WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid
          AND (l.legacy_unbound
            OR (l.historical_revision IS NOT NULL AND
                (h.revision IS DISTINCT FROM l.historical_revision
                 OR (l.next_expiry IS NOT NULL AND l.next_expiry<=clock_timestamp())))
            OR (cardinality(l.preference_keys)>0 AND l.preference_revisions IS DISTINCT FROM
                (SELECT COALESCE(jsonb_agg(jsonb_build_array(p.preference_key,p.status,p.version)
                                          ORDER BY p.preference_key),'[]'::jsonb)
                 FROM current_relationship_preferences p
                 WHERE p.user_id=l.user_id AND p.instance_id=l.instance_id
                   AND p.scope_key='' AND p.preference_key=ANY(l.preference_keys))))
    ) AND response_raw_lineage_current(rid,uid,iid)
      AND response_entity_lineage_current(rid,uid,iid);
$$;
