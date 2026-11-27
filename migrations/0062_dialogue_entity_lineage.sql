-- MVP-07B2.7. Reuse scoped owner sources, not a second dialogue store.
-- The current relation source survives alias additions; superseded relation
-- and old alias revisions do not justify generic raw/history copies.
CREATE FUNCTION entity_turn_current(uid uuid,iid uuid,cid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM memory_entity_revisions rev
        JOIN memory_entities e ON e.entity_id=rev.entity_id
          AND e.user_id=rev.user_id AND e.instance_id=rev.instance_id
        WHERE rev.user_id=uid AND rev.instance_id=iid
          AND rev.source_conversation_id=cid AND rev.source_turn_id=sid
          AND (e.record_status<>'ACTIVE' OR e.privacy_class<>'NORMAL'
               OR e.expires_at<=clock_timestamp()
               OR (rev.version<>e.version AND rev.source_turn_id<>e.relation_source_turn_id))
    ) AND NOT EXISTS (
        SELECT 1 FROM memory_entity_relationship_revisions rev
        JOIN memory_entity_relationships r ON r.relationship_id=rev.relationship_id
          AND r.user_id=rev.user_id AND r.instance_id=rev.instance_id
        JOIN memory_entities s ON s.entity_id=r.subject_entity_id
          AND s.user_id=r.user_id AND s.instance_id=r.instance_id
        JOIN memory_entities o ON o.entity_id=r.object_entity_id
          AND o.user_id=r.user_id AND o.instance_id=r.instance_id
        WHERE rev.user_id=uid AND rev.instance_id=iid
          AND rev.source_conversation_id=cid AND rev.source_turn_id=sid
          AND (rev.version<>r.version OR r.record_status<>'ACTIVE'
               OR s.record_status<>'ACTIVE' OR s.privacy_class<>'NORMAL'
               OR s.expires_at<=clock_timestamp()
               OR o.record_status<>'ACTIVE' OR o.privacy_class<>'NORMAL'
               OR o.expires_at<=clock_timestamp())
    ) AND NOT EXISTS (
        -- Recognition of an existing relation is not another revision.
        -- Its trusted report response records the exact identity/version.
        -- Do not use a stale recognition turn as generic evidence, but do
        -- not invalidate the current owner relation source just because
        -- its original response predates an alias addition.
        SELECT 1 FROM events ev
        JOIN conversation_responses r ON r.response_id::text=ev.payload->>'response_id'
          AND r.user_id=ev.user_id AND r.instance_id=ev.instance_id
        WHERE ev.user_id=uid AND ev.instance_id=iid
          AND r.conversation_id=cid AND r.turn_id=sid
          AND ev.event_type='conversation.response.entity_grounded.v1'
          AND ev.trust_level='TRUSTED_PLATFORM' AND ev.payload->>'status'='KNOWN'
          AND (r.model_id='conversation.entity_report.v1' OR ev.payload->>'entity_report'='true')
          AND NOT EXISTS (SELECT 1 FROM memory_entity_revisions rev
              WHERE rev.user_id=uid AND rev.instance_id=iid
                AND rev.source_conversation_id=cid AND rev.source_turn_id=sid)
          AND NOT EXISTS (SELECT 1 FROM memory_entities e
              WHERE e.user_id=uid AND e.instance_id=iid
                AND e.entity_id::text=ev.payload->>'entity_id'
                AND e.version::text=ev.payload->>'entity_version'
                AND e.record_status='ACTIVE' AND e.privacy_class='NORMAL'
                AND (e.expires_at IS NULL OR e.expires_at>clock_timestamp()))
    ) AND NOT EXISTS (
        SELECT 1 FROM memory_entity_evidence_links l
        JOIN memory_entities e ON e.entity_id=l.entity_id
          AND e.user_id=l.user_id AND e.instance_id=l.instance_id
        WHERE l.user_id=uid AND l.instance_id=iid
          AND l.source_conversation_id=cid AND l.source_turn_id=sid
          AND (e.record_status<>'ACTIVE' OR e.privacy_class<>'NORMAL'
               OR e.expires_at<=clock_timestamp())
    )
$$;

CREATE OR REPLACE FUNCTION raw_context_eligible_turns(uid uuid,iid uuid,cid uuid,ids uuid[])
RETURNS SETOF uuid LANGUAGE sql AS $$
    SELECT t.turn_id FROM conversation_turns t
    WHERE t.user_id=uid AND t.instance_id=iid AND t.conversation_id=cid
      AND t.turn_id=ANY(ids)
      AND (
        (
      NOT EXISTS (
        SELECT 1 FROM memory_meaning_change_notices notice
        WHERE notice.user_id=t.user_id AND notice.instance_id=t.instance_id
          AND notice.source_turn_id=t.turn_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM explicit_memory_reports report
        LEFT JOIN memory_records linked
          ON linked.user_id=report.user_id AND linked.instance_id=report.instance_id
         AND linked.memory_kind=report.memory_kind
         AND linked.subject_key=report.subject_key AND linked.predicate_key=report.predicate_key
        JOIN conversation_turns origin
          ON origin.turn_id=report.source_turn_id
         AND origin.conversation_id=report.conversation_id
         AND origin.user_id=report.user_id AND origin.instance_id=report.instance_id
        WHERE report.user_id=t.user_id AND report.instance_id=t.instance_id
          AND report.source_turn_id=t.turn_id
          AND (
              (linked.memory_id IS NOT NULL AND (
                  NOT ((
        linked.record_status='ACTIVE' AND linked.privacy_class='NORMAL'
        AND linked.epistemic_status='USER_REPORTED'
        AND (linked.expires_at IS NULL OR linked.expires_at>clock_timestamp())
        AND NOT EXISTS (
        SELECT 1 FROM memory_entity_evidence_links el
        JOIN memory_entities en ON en.entity_id=el.entity_id
          AND en.user_id=el.user_id AND en.instance_id=el.instance_id
        WHERE el.user_id=linked.user_id AND el.instance_id=linked.instance_id
          AND el.source_turn_id=linked.source_turn_id
          AND (en.record_status<>'ACTIVE' OR en.privacy_class<>'NORMAL'
               OR (en.expires_at IS NOT NULL AND en.expires_at<=clock_timestamp()))
    )
        AND NOT EXISTS (
        SELECT 1 FROM memory_entity_relationship_revisions er
        WHERE er.user_id=linked.user_id AND er.instance_id=linked.instance_id
          AND er.source_turn_id=linked.source_turn_id
    )
    ) AND NOT (
      (
        linked.memory_kind='PERSONAL_MEANING' AND linked.subject_key='user'
        AND EXISTS (
            SELECT 1 FROM memory_meaning_change_notices notice
            JOIN conversation_turns n ON n.turn_id=notice.source_turn_id
              AND n.conversation_id=notice.conversation_id
              AND n.user_id=notice.user_id AND n.instance_id=notice.instance_id
            WHERE notice.user_id=linked.user_id AND notice.instance_id=linked.instance_id
              AND notice.predicate_key=linked.predicate_key
              AND n.created_at>=linked.source_turn_at
              -- An identical, owner-validated reconfirmation may resolve
              -- this notice without rewriting the original revision.
              AND NOT EXISTS (
                  SELECT 1 FROM memory_claim_support support
                  JOIN memory_extraction_candidates c
                    ON c.target_memory_id=support.memory_id
                   AND c.target_version=support.version
                   AND c.user_id=support.user_id AND c.instance_id=support.instance_id
                   AND c.conversation_id=support.conversation_id
                   AND c.source_turn_id=support.source_turn_id
                   AND c.source_quote=support.source_quote
                  JOIN conversation_turns confirmed
                    ON confirmed.turn_id=support.source_turn_id
                   AND confirmed.conversation_id=support.conversation_id
                   AND confirmed.user_id=support.user_id AND confirmed.instance_id=support.instance_id
                   AND confirmed.source_input_event_id=c.source_event_id
                  WHERE support.memory_id=linked.memory_id AND support.version=linked.version
                    AND support.user_id=linked.user_id AND support.instance_id=linked.instance_id
                    AND c.status='CORROBORATED' AND c.memory_kind=linked.memory_kind
                    AND c.subject_key=linked.subject_key AND c.predicate_key=linked.predicate_key
                    AND c.value_text=linked.value_text
                    AND confirmed.created_at>n.created_at
              )
        )
    )
      OR EXISTS (
        SELECT 1 FROM memory_extraction_candidates unresolved
        JOIN conversation_turns newer_turn
          ON newer_turn.turn_id=unresolved.source_turn_id
         AND newer_turn.conversation_id=unresolved.conversation_id
         AND newer_turn.user_id=unresolved.user_id
         AND newer_turn.instance_id=unresolved.instance_id
        WHERE unresolved.target_memory_id=linked.memory_id
          AND unresolved.user_id=linked.user_id
          AND unresolved.instance_id=linked.instance_id
          AND unresolved.status='CONFLICT'
          AND newer_turn.created_at>linked.source_turn_at
          AND unresolved.value_text<>linked.value_text
      )
      OR EXISTS (
        SELECT 1 FROM explicit_memory_reports report
        JOIN conversation_turns newer
          ON newer.turn_id=report.source_turn_id
         AND newer.conversation_id=report.conversation_id
         AND newer.user_id=report.user_id AND newer.instance_id=report.instance_id
        WHERE report.user_id=linked.user_id AND report.instance_id=linked.instance_id
          AND report.memory_kind=linked.memory_kind
          AND report.subject_key=linked.subject_key AND report.predicate_key=linked.predicate_key
          AND newer.created_at>linked.source_turn_at
          AND report.value_text<>linked.value_text
      )
    )) OR linked.value_text<>report.value_text
              ))
              OR (report.memory_kind='PERSONAL_MEANING' AND EXISTS (
                  SELECT 1 FROM memory_meaning_change_notices notice
                  JOIN conversation_turns newer ON newer.turn_id=notice.source_turn_id
                    AND newer.conversation_id=notice.conversation_id
                    AND newer.user_id=notice.user_id AND newer.instance_id=notice.instance_id
                  WHERE notice.user_id=report.user_id AND notice.instance_id=report.instance_id
                    AND notice.predicate_key=report.predicate_key
                    AND newer.created_at>=origin.created_at
              ))
              OR EXISTS (
                  SELECT 1 FROM explicit_memory_reports later
                  JOIN conversation_turns newer
                    ON newer.turn_id=later.source_turn_id
                   AND newer.conversation_id=later.conversation_id
                   AND newer.user_id=later.user_id AND newer.instance_id=later.instance_id
                  WHERE later.user_id=report.user_id AND later.instance_id=report.instance_id
                    AND later.memory_kind=report.memory_kind
                    AND later.subject_key=report.subject_key AND later.predicate_key=report.predicate_key
                    AND newer.created_at>origin.created_at
                    AND later.value_text<>report.value_text
              )
          )
      )
    )
        AND NOT EXISTS (
            SELECT 1 FROM memory_revisions mr
            JOIN memory_records linked ON linked.memory_id=mr.memory_id
              AND linked.user_id=mr.user_id AND linked.instance_id=mr.instance_id
            WHERE mr.user_id=t.user_id AND mr.instance_id=t.instance_id
              AND mr.source_conversation_id=t.conversation_id
              AND mr.source_turn_id=t.turn_id
              AND (mr.version<>linked.version OR NOT ((
        linked.record_status='ACTIVE' AND linked.privacy_class='NORMAL'
        AND linked.epistemic_status='USER_REPORTED'
        AND (linked.expires_at IS NULL OR linked.expires_at>clock_timestamp())
        AND NOT EXISTS (
        SELECT 1 FROM memory_entity_evidence_links el
        JOIN memory_entities en ON en.entity_id=el.entity_id
          AND en.user_id=el.user_id AND en.instance_id=el.instance_id
        WHERE el.user_id=linked.user_id AND el.instance_id=linked.instance_id
          AND el.source_turn_id=linked.source_turn_id
          AND (en.record_status<>'ACTIVE' OR en.privacy_class<>'NORMAL'
               OR (en.expires_at IS NOT NULL AND en.expires_at<=clock_timestamp()))
    )
        AND NOT EXISTS (
        SELECT 1 FROM memory_entity_relationship_revisions er
        WHERE er.user_id=linked.user_id AND er.instance_id=linked.instance_id
          AND er.source_turn_id=linked.source_turn_id
    )
    ) AND NOT (
      (
        linked.memory_kind='PERSONAL_MEANING' AND linked.subject_key='user'
        AND EXISTS (
            SELECT 1 FROM memory_meaning_change_notices notice
            JOIN conversation_turns n ON n.turn_id=notice.source_turn_id
              AND n.conversation_id=notice.conversation_id
              AND n.user_id=notice.user_id AND n.instance_id=notice.instance_id
            WHERE notice.user_id=linked.user_id AND notice.instance_id=linked.instance_id
              AND notice.predicate_key=linked.predicate_key
              AND n.created_at>=linked.source_turn_at
              -- An identical, owner-validated reconfirmation may resolve
              -- this notice without rewriting the original revision.
              AND NOT EXISTS (
                  SELECT 1 FROM memory_claim_support support
                  JOIN memory_extraction_candidates c
                    ON c.target_memory_id=support.memory_id
                   AND c.target_version=support.version
                   AND c.user_id=support.user_id AND c.instance_id=support.instance_id
                   AND c.conversation_id=support.conversation_id
                   AND c.source_turn_id=support.source_turn_id
                   AND c.source_quote=support.source_quote
                  JOIN conversation_turns confirmed
                    ON confirmed.turn_id=support.source_turn_id
                   AND confirmed.conversation_id=support.conversation_id
                   AND confirmed.user_id=support.user_id AND confirmed.instance_id=support.instance_id
                   AND confirmed.source_input_event_id=c.source_event_id
                  WHERE support.memory_id=linked.memory_id AND support.version=linked.version
                    AND support.user_id=linked.user_id AND support.instance_id=linked.instance_id
                    AND c.status='CORROBORATED' AND c.memory_kind=linked.memory_kind
                    AND c.subject_key=linked.subject_key AND c.predicate_key=linked.predicate_key
                    AND c.value_text=linked.value_text
                    AND confirmed.created_at>n.created_at
              )
        )
    )
      OR EXISTS (
        SELECT 1 FROM memory_extraction_candidates unresolved
        JOIN conversation_turns newer_turn
          ON newer_turn.turn_id=unresolved.source_turn_id
         AND newer_turn.conversation_id=unresolved.conversation_id
         AND newer_turn.user_id=unresolved.user_id
         AND newer_turn.instance_id=unresolved.instance_id
        WHERE unresolved.target_memory_id=linked.memory_id
          AND unresolved.user_id=linked.user_id
          AND unresolved.instance_id=linked.instance_id
          AND unresolved.status='CONFLICT'
          AND newer_turn.created_at>linked.source_turn_at
          AND unresolved.value_text<>linked.value_text
      )
      OR EXISTS (
        SELECT 1 FROM explicit_memory_reports report
        JOIN conversation_turns newer
          ON newer.turn_id=report.source_turn_id
         AND newer.conversation_id=report.conversation_id
         AND newer.user_id=report.user_id AND newer.instance_id=report.instance_id
        WHERE report.user_id=linked.user_id AND report.instance_id=linked.instance_id
          AND report.memory_kind=linked.memory_kind
          AND report.subject_key=linked.subject_key AND report.predicate_key=linked.predicate_key
          AND newer.created_at>linked.source_turn_at
          AND report.value_text<>linked.value_text
      )
    )))
        )
        AND entity_turn_current(t.user_id,t.instance_id,t.conversation_id,t.turn_id)
    )
$$;


-- Canonical entity report writes cannot capture the scope revision before
-- their own deferred owner revision bump. Reuse their trusted exact event
-- pins instead at every controlled replay/delivery and derived boundary.
CREATE FUNCTION entity_response_has_evidence(rid uuid,uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM events e WHERE e.user_id=uid AND e.instance_id=iid
          AND e.event_type='conversation.response.entity_grounded.v1'
          AND e.trust_level='TRUSTED_PLATFORM' AND e.payload->>'response_id'=rid::text
          AND (e.payload->>'status'='KNOWN'
               OR CASE WHEN jsonb_typeof(e.payload->'clarification_candidates')='array'
                   THEN jsonb_array_length(e.payload->'clarification_candidates')>0 ELSE false END)
    )
$$;

CREATE FUNCTION entity_revision_current(uid uuid,iid uuid,eid text,ver text)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM memory_entities m
        WHERE m.user_id=uid AND m.instance_id=iid
          AND m.entity_id::text=eid AND m.version::text=ver
          AND m.record_status='ACTIVE' AND m.privacy_class='NORMAL'
          AND (m.expires_at IS NULL OR m.expires_at>clock_timestamp())
          AND raw_context_sources_current(uid,iid,m.relation_source_conversation_id,
                                           ARRAY[m.relation_source_turn_id])
    )
$$;

CREATE FUNCTION response_entity_lineage_current(rid uuid,uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM events e WHERE e.user_id=uid AND e.instance_id=iid
          AND e.event_type='conversation.response.entity_grounded.v1'
          AND e.trust_level='TRUSTED_PLATFORM' AND e.payload->>'response_id'=rid::text
          AND (
            (e.payload->>'status'='KNOWN' AND NOT entity_revision_current(
                uid,iid,e.payload->>'entity_id',e.payload->>'entity_version'))
            OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(CASE
                    WHEN jsonb_typeof(e.payload->'clarification_candidates')='array'
                    THEN e.payload->'clarification_candidates' ELSE '[]'::jsonb END) choice
                WHERE NOT entity_revision_current(uid,iid,choice->>0,choice->>1)
            )
          )
    )
$$;

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
                 FROM relationship_preferences p
                 WHERE p.user_id=l.user_id AND p.instance_id=l.instance_id
                   AND p.scope_key='' AND p.preference_key=ANY(l.preference_keys))))
    ) AND response_raw_lineage_current(rid,uid,iid)
      AND response_entity_lineage_current(rid,uid,iid);
$$;


CREATE OR REPLACE FUNCTION response_lineage_derived_text(rid uuid,uid uuid,iid uuid,
                                              excerpt boolean,body text)
RETURNS text LANGUAGE sql AS $$
    SELECT CASE WHEN excerpt THEN '[Saved document excerpts omitted; request fresh recall.]'
           WHEN EXISTS (SELECT 1 FROM conversation_response_lineage l
                        WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid)
                OR entity_response_has_evidence(rid,uid,iid)
           THEN '[Evidence-bearing assistant text omitted; request fresh recall.]'
           ELSE body END;
$$;

-- No guessed next-action provenance for pre-0062 rows. Historical strings
-- remain stored; a freshly authored hint supplies its own source.
ALTER TABLE dialogue_threads ADD COLUMN next_action_turn_id uuid;
ALTER TABLE dialogue_threads ADD COLUMN next_action_response_id uuid;
ALTER TABLE dialogue_threads ADD CONSTRAINT dialogue_next_action_turn_fk
    FOREIGN KEY(conversation_id,next_action_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id);
ALTER TABLE dialogue_threads ADD CONSTRAINT dialogue_next_action_response_fk
    FOREIGN KEY(conversation_id,next_action_response_id)
        REFERENCES conversation_responses(conversation_id,response_id);

CREATE FUNCTION dialogue_turn_current(uid uuid,iid uuid,cid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT sid IS NOT NULL AND raw_context_sources_current(uid,iid,cid,ARRAY[sid])
$$;

-- An assistant label inherits its actual prior response's raw dependencies.
-- Preference-dependent or entity-grounded bodies are not an independent
-- dialogue source. Broad historical dependencies carry a separate flag.
CREATE FUNCTION dialogue_response_sources(uid uuid,iid uuid,cid uuid,rid uuid)
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
            AND cardinality(l.preference_keys)>0)
      AND dialogue_turn_current(uid,iid,cid,r.turn_id)
      AND response_lineage_current(rid,uid,iid)
$$;

CREATE FUNCTION dialogue_response_broad(uid uuid,iid uuid,rid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM conversation_response_lineage l
        WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid)
$$;

CREATE FUNCTION dialogue_signal_sources(uid uuid,iid uuid,cid uuid,sid uuid,rid uuid)
RETURNS uuid[] LANGUAGE sql AS $$
    SELECT CASE WHEN dialogue_turn_current(uid,iid,cid,sid)
        THEN CASE WHEN rid IS NULL THEN ARRAY[sid]
             ELSE dialogue_response_sources(uid,iid,cid,rid) END
        ELSE NULL::uuid[] END
$$;

-- References are one-hop links to an objective, not independently
-- authorized intent. A correction's original user text is still preserved.
CREATE VIEW current_dialogue_intents AS
    SELECT s.*,ARRAY(SELECT DISTINCT x FROM unnest(
            p.source_ids||COALESCE(root_sources.source_ids,'{}'::uuid[])) x ORDER BY x) AS source_ids,
           (dialogue_response_broad(s.user_id,s.instance_id,s.source_response_id)
            OR dialogue_response_broad(s.user_id,s.instance_id,root.source_response_id)) AS broad_dependency
    FROM conversation_intent_signals s
    LEFT JOIN conversation_intent_signals root ON root.signal_id=s.related_signal_id
      AND root.user_id=s.user_id AND root.instance_id=s.instance_id
      AND root.conversation_id=s.conversation_id AND root.intent_kind='OBJECTIVE'
      AND root.related_signal_id IS NULL
    CROSS JOIN LATERAL (SELECT dialogue_signal_sources(
        s.user_id,s.instance_id,s.conversation_id,s.turn_id,s.source_response_id) AS source_ids) p
    CROSS JOIN LATERAL (SELECT dialogue_signal_sources(
        root.user_id,root.instance_id,root.conversation_id,root.turn_id,root.source_response_id) AS source_ids) root_sources
    WHERE p.source_ids IS NOT NULL
      AND (s.related_signal_id IS NULL OR root_sources.source_ids IS NOT NULL);

CREATE VIEW current_dialogue_acts AS
    SELECT a.*,p.source_ids,
           dialogue_response_broad(a.user_id,a.instance_id,a.source_response_id) AS broad_dependency
    FROM dialogue_pending_acts a
    CROSS JOIN LATERAL (SELECT dialogue_response_sources(
        a.user_id,a.instance_id,a.conversation_id,a.source_response_id) AS source_ids) p
    WHERE p.source_ids IS NOT NULL;

CREATE VIEW current_dialogue_threads AS
    SELECT d.thread_id,d.conversation_id,d.user_id,d.instance_id,d.title,
           d.attention_status,d.resolution_status,d.origin_turn_id,d.last_turn_id,
           d.revision,d.created_at,d.updated_at,d.origin_kind,
           CASE WHEN hint.source_ids IS NOT NULL THEN d.next_conversational_action END
                AS next_conversational_action,
           ARRAY(SELECT DISTINCT x FROM unnest(
               COALESCE(s.source_ids,ARRAY[d.origin_turn_id])||
               COALESCE(hint.source_ids,'{}'::uuid[])) x ORDER BY x) AS source_ids,
           (COALESCE(s.broad_dependency,false) OR
            (hint.source_ids IS NOT NULL AND dialogue_response_broad(
                d.user_id,d.instance_id,d.next_action_response_id))) AS broad_dependency
    FROM dialogue_threads d
    -- Select the origin signal for this thread before evaluating lineage.
    -- OFFSET 0 prevents flattening into a database-wide eligibility join.
    LEFT JOIN LATERAL (
        SELECT s.* FROM current_dialogue_intents s
        WHERE s.turn_id=d.origin_turn_id
          AND s.conversation_id=d.conversation_id AND s.user_id=d.user_id
          AND s.instance_id=d.instance_id AND d.origin_kind='AUTO_SIGNAL'
        OFFSET 0
    ) s ON true
    CROSS JOIN LATERAL (SELECT CASE
        WHEN d.next_conversational_action IS NULL THEN NULL::uuid[]
        WHEN d.next_action_response_id IS NOT NULL THEN dialogue_response_sources(
            d.user_id,d.instance_id,d.conversation_id,d.next_action_response_id)
        WHEN dialogue_turn_current(d.user_id,d.instance_id,d.conversation_id,d.next_action_turn_id)
            THEN ARRAY[d.next_action_turn_id]
        ELSE NULL::uuid[] END AS source_ids) hint
    WHERE dialogue_turn_current(d.user_id,d.instance_id,d.conversation_id,d.origin_turn_id)
      AND (d.origin_kind<>'AUTO_SIGNAL' OR s.signal_id IS NOT NULL);

CREATE VIEW current_dialogue_referents AS
    SELECT r.*,ARRAY[r.source_turn_id] AS source_ids,false AS broad_dependency
    FROM dialogue_referents r
    WHERE dialogue_turn_current(r.user_id,r.instance_id,r.conversation_id,r.source_turn_id);
