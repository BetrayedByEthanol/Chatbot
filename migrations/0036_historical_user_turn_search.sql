-- MVP-02B.1: derived, rebuildable lookup indexes on authoritative
-- authenticated Conversation user turns. No new memory owner or data copy.
-- English FTS is deliberately separate from the existing Memory 'simple'
-- indexes: stemming handles "walk" / "walking" for direct recall queries.
CREATE INDEX conversation_turns_prior_session_fts_idx
    ON conversation_turns USING gin (to_tsvector('english', input_text))
    WHERE status='RESPONDED';

CREATE INDEX conversation_turns_prior_session_scope_idx
    ON conversation_turns(user_id, instance_id, created_at DESC)
    WHERE status='RESPONDED';

CREATE INDEX memory_revisions_source_turn_scope_idx
    ON memory_revisions(user_id, instance_id, source_turn_id);
