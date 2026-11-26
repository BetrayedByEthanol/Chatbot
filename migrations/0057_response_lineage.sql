-- MVP-07B2: persist the read fence with the derived response. This is
-- eligibility metadata, not a second Memory owner or a delivery status.
CREATE TABLE conversation_response_lineage (
    response_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    historical_revision bigint CHECK(historical_revision>=0),
    next_expiry timestamptz,
    preference_keys text[] NOT NULL DEFAULT '{}',
    preference_revisions jsonb NOT NULL DEFAULT '[]'::jsonb,
    legacy_unbound boolean NOT NULL DEFAULT false,
    CHECK(next_expiry IS NULL OR historical_revision IS NOT NULL),
    CHECK(cardinality(preference_keys)<=4),
    CHECK(jsonb_typeof(preference_revisions)='array'
          AND jsonb_array_length(preference_revisions)<=4),
    CHECK((legacy_unbound AND historical_revision IS NULL
           AND cardinality(preference_keys)=0 AND preference_revisions='[]'::jsonb)
          OR (NOT legacy_unbound AND
              (historical_revision IS NOT NULL OR cardinality(preference_keys)>0))),
    FOREIGN KEY(response_id,user_id,instance_id)
        REFERENCES conversation_responses(response_id,user_id,instance_id)
);
-- Old outputs lack a durable complete read fence. Do not invent a
-- historical revision, erase the original, or silently authorize replay.
INSERT INTO conversation_response_lineage(response_id,user_id,instance_id,legacy_unbound)
    SELECT response_id,user_id,instance_id,true FROM conversation_responses;

-- Snapshot-only projection for metadata and worker readiness. Delivery
-- additionally takes the existing owner locks and checks the same fence.
CREATE FUNCTION response_lineage_current(rid uuid,uid uuid,iid uuid)
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
    );
$$;
-- Use the same omission across recent model context, summary and cognition.
-- A tracked response is not a new independent source even while current.
CREATE FUNCTION response_lineage_derived_text(rid uuid,uid uuid,iid uuid,
                                              excerpt boolean,body text)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN excerpt THEN '[Saved document excerpts omitted; request fresh recall.]'
           WHEN EXISTS (SELECT 1 FROM conversation_response_lineage l
                        WHERE l.response_id=rid AND l.user_id=uid AND l.instance_id=iid)
           THEN '[Evidence-bearing assistant text omitted; request fresh recall.]'
           ELSE body END;
$$;
CREATE TRIGGER immutable_response_lineage
    BEFORE UPDATE OR DELETE ON conversation_response_lineage
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
