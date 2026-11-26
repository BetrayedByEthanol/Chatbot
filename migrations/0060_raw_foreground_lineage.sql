-- MVP-07B2: exact bounded raw-context lineage, separate from broad
-- historical recall and Relationship fences. No copied source content.
CREATE TABLE conversation_response_raw_lineage (
    response_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_ids uuid[] NOT NULL,
    -- The current authenticated input is never hidden. When it was already
    -- ineligible at preparation, its reply may be delivered but not reused.
    reusable boolean NOT NULL,
    legacy_unbound boolean NOT NULL DEFAULT false,
    CHECK(NOT legacy_unbound OR (NOT reusable AND cardinality(source_turn_ids)=0)),
    CHECK(cardinality(source_turn_ids)<=128
          AND array_position(source_turn_ids,NULL) IS NULL),
    FOREIGN KEY(response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id),
    FOREIGN KEY(conversation_id,response_id)
        REFERENCES conversation_responses(conversation_id,response_id),
    FOREIGN KEY(conversation_id,user_id,instance_id)
        REFERENCES conversations(conversation_id,user_id,instance_id)
);
CREATE TRIGGER immutable_raw_response_lineage
    BEFORE UPDATE OR DELETE ON conversation_response_raw_lineage
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();

-- Existing ordinary responses have no recorded full raw input set. Do not
-- fabricate dependencies or authorize replay/derived reuse from these rows.
-- Existing broad-fenced responses retain their previous lineage contract.
INSERT INTO conversation_response_raw_lineage(
    response_id,user_id,instance_id,conversation_id,source_turn_ids,reusable,legacy_unbound
)
SELECT r.response_id,r.user_id,r.instance_id,r.conversation_id,'{}'::uuid[],false,true
FROM conversation_responses r
WHERE NOT EXISTS (
    SELECT 1 FROM conversation_response_lineage l
    WHERE l.response_id=r.response_id AND l.user_id=r.user_id AND l.instance_id=r.instance_id
);

-- SQL counterpart of Memory's eligible_summary_turn_sql(). Keep the exact
-- owner policy in sync (contract test compares the expanded predicate).
-- The set-based boundary avoids a new query for each selected source.
CREATE FUNCTION raw_context_eligible_turns(uid uuid,iid uuid,cid uuid,ids uuid[])
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
    )
$$;

CREATE FUNCTION raw_context_sources_current(uid uuid,iid uuid,cid uuid,ids uuid[])
RETURNS boolean LANGUAGE sql AS $$
    SELECT cardinality(ids)=(SELECT count(*) FROM raw_context_eligible_turns(uid,iid,cid,ids))
$$;

CREATE FUNCTION response_raw_lineage_current(rid uuid,uid uuid,iid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM conversation_response_raw_lineage raw
        WHERE raw.response_id=rid AND raw.user_id=uid AND raw.instance_id=iid
          AND (raw.legacy_unbound
               OR NOT raw_context_sources_current(uid,iid,raw.conversation_id,raw.source_turn_ids))
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
    ) AND response_raw_lineage_current(rid,uid,iid);
$$;

-- Delivery's derived dialogue data must not reuse an initially ineligible
-- anchor or an invalidated raw response. The original delivery is unchanged.
CREATE FUNCTION response_lineage_foreground_text(rid uuid,uid uuid,iid uuid,
                                                excerpt boolean,body text)
RETURNS text LANGUAGE sql AS $$
    SELECT CASE WHEN excerpt THEN response_lineage_derived_text(rid,uid,iid,excerpt,body)
           WHEN EXISTS (
               SELECT 1 FROM conversation_response_raw_lineage raw
               WHERE raw.response_id=rid AND raw.user_id=uid AND raw.instance_id=iid
                 AND (NOT raw.reusable OR NOT response_raw_lineage_current(rid,uid,iid))
           ) THEN '[Evidence-bearing assistant text omitted; request fresh recall.]'
           ELSE response_lineage_derived_text(rid,uid,iid,excerpt,body) END
$$;

-- Summary/cognition persist their own input ranges. Only reuse raw-derived
-- assistant bodies if every exact dependency belongs to that range and is
-- current; otherwise their source set would silently lose provenance.
CREATE FUNCTION response_lineage_range_text(rid uuid,uid uuid,iid uuid,
                                          excerpt boolean,body text,
                                          first_sequence bigint,last_sequence bigint)
RETURNS text LANGUAGE sql AS $$
    SELECT CASE WHEN excerpt THEN response_lineage_derived_text(rid,uid,iid,excerpt,body)
           WHEN EXISTS (
               SELECT 1 FROM conversation_response_raw_lineage raw
               WHERE raw.response_id=rid AND raw.user_id=uid AND raw.instance_id=iid
                 AND (NOT raw.reusable
                      OR (SELECT count(*) FROM conversation_turns t
                          WHERE t.user_id=uid AND t.instance_id=iid
                            AND t.conversation_id=raw.conversation_id
                            AND t.turn_id=ANY(raw.source_turn_ids)
                            AND t.sequence BETWEEN first_sequence AND last_sequence)
                           <>cardinality(raw.source_turn_ids)
                      OR NOT response_raw_lineage_current(rid,uid,iid))
           ) THEN '[Evidence-bearing assistant text omitted; request fresh recall.]'
           ELSE response_lineage_derived_text(rid,uid,iid,excerpt,body) END
$$;
