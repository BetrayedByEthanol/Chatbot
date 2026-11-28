-- P2-F1.1: Governance owns controls, not predictive content.
-- Absence is disabled, generation 0. No historical backfill.
CREATE TABLE predictive_learning_controls (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    learning_enabled boolean NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    reset_boundary_ref uuid,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_event_id uuid NOT NULL,
    PRIMARY KEY(user_id,instance_id),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY(source_event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(reset_boundary_ref,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    CHECK((reset_generation=0)=(reset_boundary_ref IS NULL))
);

-- Only live authenticated text ingress stamps an eligible generation.
-- The selection lock serializes ingress with control transitions; this
-- is a scope-wide causal cutoff without a new global event sequence.
CREATE TABLE predictive_input_admissions (
    turn_id uuid PRIMARY KEY REFERENCES conversation_turns(turn_id),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    source_event_id uuid NOT NULL UNIQUE,
    admitted_control_version bigint NOT NULL CHECK(admitted_control_version>=1),
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    FOREIGN KEY(source_event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY(user_id,instance_id) REFERENCES predictive_learning_controls(user_id,instance_id)
);
CREATE TRIGGER immutable_predictive_input_admission
    BEFORE UPDATE OR DELETE ON predictive_input_admissions
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();

-- Initially only exact raw authenticated text input is supported.
-- Model interpretations, summaries, patterns, outcomes, feedback and
-- other derived sources must acquire owner-specific admission later.
CREATE FUNCTION predictive_sources_current(uid uuid,iid uuid,gen bigint,ids uuid[])
RETURNS boolean LANGUAGE sql AS $$
    SELECT COALESCE(cardinality(ids) BETWEEN 1 AND 128
        AND cardinality(ids)=(SELECT count(*) FROM predictive_input_admissions p
          JOIN conversation_turns t ON t.turn_id=p.turn_id
            AND t.user_id=p.user_id AND t.instance_id=p.instance_id
            AND t.source_input_event_id=p.source_event_id
          JOIN events e ON e.event_id=p.source_event_id
            AND e.user_id=p.user_id AND e.instance_id=p.instance_id
          WHERE p.user_id=uid AND p.instance_id=iid
            AND p.reset_generation=gen AND p.turn_id=ANY(ids)
            AND e.event_type='conversation.user_text.received.v1'
            AND e.trust_level='AUTHENTICATED_USER'
            AND e.payload->>'turn_id'=t.turn_id::text
            AND e.payload->>'conversation_id'=t.conversation_id::text
            AND NOT (e.payload ? 'input_modality')
            AND dialogue_turn_current(t.user_id,t.instance_id,t.conversation_id,t.turn_id)
        ),false)
$$;

CREATE FUNCTION predictive_fence_current(uid uuid,iid uuid,ver bigint,gen bigint,ids uuid[])
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM predictive_learning_controls c
        WHERE c.user_id=uid AND c.instance_id=iid AND c.learning_enabled
          AND c.version=ver AND c.reset_generation=gen)
        AND predictive_sources_current(uid,iid,gen,ids)
$$;

-- Reuse immutable Conversation lineage and its conservative derived-text
-- omission. No predictive output is a new independent observation.
ALTER TABLE conversation_response_lineage
    ADD COLUMN predictive_control_version bigint,
    ADD COLUMN predictive_reset_generation bigint,
    ADD COLUMN predictive_source_ids uuid[],
    ADD CONSTRAINT predictive_response_fence_complete CHECK (
        (predictive_control_version IS NULL AND predictive_reset_generation IS NULL
            AND predictive_source_ids IS NULL)
        OR (predictive_control_version IS NOT NULL AND predictive_control_version>=1
            AND predictive_reset_generation IS NOT NULL AND predictive_reset_generation>=0
            AND predictive_source_ids IS NOT NULL
            AND cardinality(predictive_source_ids) BETWEEN 1 AND 128
            AND array_position(predictive_source_ids,NULL) IS NULL
            AND historical_revision IS NOT NULL AND NOT legacy_unbound AND NOT presentation_only)
    );

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

CREATE OR REPLACE FUNCTION dialogue_response_sources(uid uuid,iid uuid,cid uuid,rid uuid)
RETURNS uuid[] LANGUAGE sql AS $$
    SELECT ARRAY(SELECT DISTINCT x FROM unnest(
                  ARRAY[r.turn_id]||COALESCE(raw.source_turn_ids,'{}'::uuid[])) x ORDER BY x)
    FROM conversation_responses r
    LEFT JOIN conversation_response_raw_lineage raw ON raw.response_id=r.response_id
      AND raw.user_id=r.user_id AND raw.instance_id=r.instance_id
    WHERE r.response_id=rid AND r.user_id=uid AND r.instance_id=iid AND r.conversation_id=cid
      AND r.delivery_status='DELIVERED'
      AND COALESCE(raw.reusable,true)
      AND NOT entity_response_has_evidence(rid,uid,iid)
      AND NOT EXISTS (SELECT 1 FROM conversation_response_lineage l
          WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid
            AND (cardinality(l.preference_keys)>0 OR l.predictive_control_version IS NOT NULL))
      AND dialogue_turn_current(uid,iid,cid,r.turn_id)
      AND response_lineage_current(rid,uid,iid)
$$;
