-- P2-F1.4: typed, conditional persona intentions in existing Cognitive owner.
-- Existing model-proposed intentions retain their original review contract.
ALTER TABLE cognitive_expectation_comparisons
    ADD UNIQUE(comparison_id,user_id,instance_id);
ALTER TABLE cognitive_intentions
    ALTER COLUMN reflection_id DROP NOT NULL,
    ADD COLUMN comparison_id uuid,
    ADD COLUMN expectation_revision_id uuid,
    ADD COLUMN condition_kind text,
    ADD COLUMN condition_context text,
    ADD UNIQUE(intention_id,user_id,instance_id),
    ADD UNIQUE(comparison_id),
    ADD FOREIGN KEY(comparison_id,user_id,instance_id)
        REFERENCES cognitive_expectation_comparisons(comparison_id,user_id,instance_id),
    ADD FOREIGN KEY(expectation_revision_id,user_id,instance_id)
        REFERENCES cognitive_expectation_revisions(revision_id,user_id,instance_id),
    ADD CHECK(
        (reflection_id IS NOT NULL AND comparison_id IS NULL
          AND expectation_revision_id IS NULL AND condition_kind IS NULL AND condition_context IS NULL)
        OR (reflection_id IS NULL AND comparison_id IS NOT NULL AND expectation_revision_id IS NOT NULL
          AND condition_kind IS NOT NULL AND condition_kind='OFFER_SUPPORT_ON_NON_COMPLETION'
          AND condition_context IS NOT NULL AND char_length(condition_context) BETWEEN 1 AND 64)
    );
CREATE INDEX cognitive_condition_intentions_idx
    ON cognitive_intentions(user_id,instance_id,condition_context,created_at DESC,intention_id)
    WHERE condition_kind IS NOT NULL AND status='PROPOSED' AND NOT already_asked;

-- The existing response lineage owns admission, not Cognitive intent fulfilment.
-- A generated projection reserves at most one opportunity; it is not proof
-- that the model asked a question or that the user received a response.
ALTER TABLE conversation_response_lineage
    ADD COLUMN subjective_expectation_id uuid,
    ADD COLUMN subjective_outcome_turn_id uuid,
    ADD COLUMN subjective_intention_id uuid,
    ADD COLUMN subjective_kind text,
    ADD COLUMN subjective_expires_at timestamptz,
    ADD FOREIGN KEY(subjective_expectation_id,user_id,instance_id)
        REFERENCES cognitive_expectation_revisions(revision_id,user_id,instance_id),
    ADD FOREIGN KEY(subjective_outcome_turn_id,user_id,instance_id)
        REFERENCES predictive_input_admissions(turn_id,user_id,instance_id),
    ADD FOREIGN KEY(subjective_intention_id,user_id,instance_id)
        REFERENCES cognitive_intentions(intention_id,user_id,instance_id),
    ADD CHECK(
        (subjective_expectation_id IS NULL AND subjective_outcome_turn_id IS NULL
          AND subjective_intention_id IS NULL AND subjective_kind IS NULL AND subjective_expires_at IS NULL)
        OR (subjective_expectation_id IS NOT NULL AND subjective_outcome_turn_id IS NOT NULL
          AND subjective_kind IS NOT NULL AND subjective_kind IN ('MISMATCH','SUPPORT')
          AND subjective_expires_at IS NOT NULL AND predictive_control_version IS NOT NULL
          AND subjective_outcome_turn_id=ANY(predictive_source_ids)
          AND (subjective_kind<>'SUPPORT' OR subjective_intention_id IS NOT NULL))
    );
CREATE UNIQUE INDEX subjective_intention_once_idx
    ON conversation_response_lineage(subjective_intention_id)
    WHERE subjective_intention_id IS NOT NULL;

-- Fail closed for optional learned presentation when a stored explicit
-- proactive boundary exists, even if its own evidence is under review.
-- This conservative pilot avoids semantic topic-boundary guessing.
CREATE FUNCTION subjective_boundary_allows(uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (SELECT 1 FROM relationship_preferences p
        WHERE p.user_id=uid AND p.instance_id=iid AND p.status='ACTIVE'
          AND (p.preference_key='AVOID_PROACTIVE_TOPIC'
            OR (p.preference_key='PROACTIVE_MESSAGES' AND p.preference_value='DISALLOW')))
$$;

-- Exact revision and report association; never use an older version as
-- fallback. Later contradictory or duplicate occasion evidence suppresses
-- the projection, not the independent authenticated input.
CREATE FUNCTION subjective_projection_current(
    uid uuid,iid uuid,ver bigint,gen bigint,ids uuid[],
    eid uuid,tid uuid,intid uuid,kind text,expiry timestamptz
) RETURNS boolean LANGUAGE sql AS $$
    SELECT COALESCE(expiry>clock_timestamp() AND subjective_boundary_allows(uid,iid)
      AND EXISTS (
        SELECT 1 FROM cognitive_expectation_revisions e
        JOIN predictive_input_admissions a ON a.turn_id=tid
          AND a.user_id=e.user_id AND a.instance_id=e.instance_id
        WHERE e.revision_id=eid AND e.user_id=uid AND e.instance_id=iid
          AND e.reset_generation=gen AND e.control_version=ver
          AND e.disposition='HYPOTHESIS' AND e.maturity='TENTATIVE'
          AND e.direction IN ('COMPLETION','NON_COMPLETION')
          AND e.expires_at>=expiry AND e.expires_at>clock_timestamp()
          AND e.created_at<a.reported_at AND a.comparison_expectation_id=eid
          AND a.admitted_control_version=ver AND a.reset_generation=gen
          AND a.reported_context=e.context_key AND a.reported_completion IS NOT NULL
          AND e.source_turn_ids <@ ids AND tid=ANY(ids)
          AND (kind='SUPPORT' OR (kind='MISMATCH'
                AND a.reported_completion<>(e.direction='COMPLETION')))
          AND NOT EXISTS (SELECT 1 FROM cognitive_expectation_revisions newer
               WHERE newer.user_id=uid AND newer.instance_id=iid AND newer.reset_generation=gen
                 AND newer.context_key=e.context_key AND newer.version>e.version)
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(e.occasion_evidence) item
               WHERE item->>'occasion'=a.reported_occasion::text)
          AND NOT EXISTS (SELECT 1 FROM predictive_input_admissions other
               WHERE other.user_id=uid AND other.instance_id=iid AND other.reset_generation=gen
                 AND other.reported_context=a.reported_context AND other.reported_occasion=a.reported_occasion
                 AND (other.reported_completion<>a.reported_completion OR
                      (other.reported_at,other.turn_id)<(a.reported_at,a.turn_id)))
          AND ((intid IS NULL AND kind='MISMATCH') OR EXISTS (
              SELECT 1 FROM cognitive_intentions i
              JOIN cognitive_expectation_comparisons c ON c.comparison_id=i.comparison_id
                AND c.user_id=i.user_id AND c.instance_id=i.instance_id
              JOIN predictive_input_admissions prior ON prior.turn_id=c.outcome_turn_id
                AND prior.user_id=c.user_id AND prior.instance_id=c.instance_id
              WHERE i.intention_id=intid AND i.user_id=uid AND i.instance_id=iid
                AND i.expectation_revision_id=eid AND i.condition_context=e.context_key
                AND i.condition_kind='OFFER_SUPPORT_ON_NON_COMPLETION'
                AND i.status='PROPOSED' AND NOT i.already_asked
                AND i.expires_at>=expiry AND i.expires_at>clock_timestamp()
                AND i.created_at<a.reported_at AND NOT a.reported_completion
                AND c.control_version=ver AND c.reset_generation=gen
                AND NOT c.completed AND c.classification IN ('EXPECTED','UNEXPECTED')
                AND c.source_turn_ids <@ ids
                AND prior.reported_occasion<>a.reported_occasion
                AND NOT EXISTS (SELECT 1 FROM predictive_input_admissions opposite
                    WHERE opposite.user_id=uid AND opposite.instance_id=iid
                      AND opposite.reset_generation=gen AND opposite.reported_context=e.context_key
                      AND opposite.reported_occasion=prior.reported_occasion
                      AND opposite.reported_completion)
          ))
      ),false)
$$;

CREATE OR REPLACE FUNCTION response_lineage_current(rid uuid,uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM conversation_response_lineage l
        LEFT JOIN historical_recall_revisions h
          ON h.user_id=l.user_id AND h.instance_id=l.instance_id
        WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid
          AND (l.legacy_unbound
            OR (l.predictive_control_version IS NOT NULL AND NOT predictive_fence_current(
                l.user_id,l.instance_id,l.predictive_control_version,
                l.predictive_reset_generation,l.predictive_source_ids))
            OR (l.subjective_expectation_id IS NOT NULL AND NOT subjective_projection_current(
                l.user_id,l.instance_id,l.predictive_control_version,l.predictive_reset_generation,
                l.predictive_source_ids,l.subjective_expectation_id,l.subjective_outcome_turn_id,
                l.subjective_intention_id,l.subjective_kind,l.subjective_expires_at))
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
