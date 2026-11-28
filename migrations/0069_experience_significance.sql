-- P2-F1.5: Memory-owned metadata about an explicit user-marked experience.
-- No copies of transcript, inferred traits, retention rules or new jobs.
CREATE TABLE memory_experience_markers (
    source_turn_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_event_id uuid NOT NULL UNIQUE,
    marked_at timestamptz NOT NULL,
    attention_until timestamptz NOT NULL,
    significance text NOT NULL DEFAULT 'USER_MARKED_IMPORTANT'
        CHECK(significance='USER_MARKED_IMPORTANT'),
    CHECK(attention_until=marked_at+interval '168 hours'),
    FOREIGN KEY(conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX memory_experience_scope_idx ON memory_experience_markers(
    user_id,instance_id,marked_at DESC,source_turn_id);
CREATE TRIGGER immutable_experience_marker
    BEFORE UPDATE OR DELETE ON memory_experience_markers
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();

-- Distinguish invalidation of the question itself from stale answer evidence.
CREATE FUNCTION memory_recall_response_current(rid uuid,uid uuid,iid uuid,cid uuid,tid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT workflow_source_current(uid,iid,cid,tid,NULL)
      AND response_lineage_current(rid,uid,iid)
      AND NOT EXISTS (
          SELECT 1 FROM conversation_response_raw_lineage raw
          WHERE raw.response_id=rid AND raw.user_id=uid AND raw.instance_id=iid
            AND NOT raw.reusable
      )
$$;

-- Latest delivered unsuppressed observation first; never skip an invalid
-- latest observation to find an older question. A repeated display without
-- a question does not extend the original cooldown.
CREATE FUNCTION memory_recall_choice_awaiting(uid uuid,iid uuid,qkey text,ckey text,cutoff timestamptz)
RETURNS boolean LANGUAGE sql AS $$
    SELECT COALESCE((
        SELECT o.outcome='AMBIGUOUS' AND o.clarification_key=ckey
          AND r.delivered_at>cutoff-interval '24 hours'
          AND memory_recall_response_current(o.response_id,o.user_id,o.instance_id,
                                             o.conversation_id,o.turn_id)
        FROM memory_recall_observations o JOIN conversation_responses r
          ON r.response_id=o.response_id AND r.user_id=o.user_id AND r.instance_id=o.instance_id
        WHERE o.user_id=uid AND o.instance_id=iid AND o.route='ENTITY'
          AND o.query_key=qkey AND r.delivery_status='DELIVERED'
          AND NOT o.clarification_suppressed AND o.outcome<>'BUDGET'
          AND r.delivered_at<=cutoff
        ORDER BY r.delivered_at DESC,o.response_id LIMIT 1
    ),false)
$$;
