-- MVP-07B2.9: exact existing source dependencies across owner boundaries.
-- Memory admission is NOT generic historical recall: a new conflicting
-- explicit report must reach Memory's review gate, not be suppressed by
-- its own unresolved slot. Committed revision/identity invalidation remains
-- mandatory. The predicate below shares the committed-source part of 0062.
CREATE FUNCTION extraction_source_current(uid uuid,iid uuid,cid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM conversation_turns t
        WHERE t.user_id=uid AND t.instance_id=iid AND t.conversation_id=cid
          AND t.turn_id=sid
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

CREATE FUNCTION goal_evidence_current(uid uuid,iid uuid,gid uuid,eids uuid[])
RETURNS boolean LANGUAGE sql AS $$
    SELECT cardinality(eids)=(SELECT count(*) FROM goal_evidence e
      WHERE e.user_id=uid AND e.instance_id=iid AND e.goal_id=gid
        AND e.evidence_id=ANY(eids)
        AND dialogue_turn_current(e.user_id,e.instance_id,
                                  e.source_conversation_id,e.source_turn_id))
$$;

-- An exact bounded set, not a new inference authority. Null means legacy
-- or trusted deterministic decomposition; old background proposals fail
-- closed via their immutable job association rather than guessed pins.
ALTER TABLE goal_subgoal_proposals ADD COLUMN input_evidence_ids uuid[];
ALTER TABLE goal_subgoal_proposals ADD CONSTRAINT goal_proposal_evidence_bound
    CHECK (input_evidence_ids IS NULL OR cardinality(input_evidence_ids)<=3);

CREATE FUNCTION goal_proposal_evidence_current(uid uuid,iid uuid,pid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM goal_subgoal_proposals p
       WHERE p.user_id=uid AND p.instance_id=iid AND p.proposal_id=pid
       AND CASE WHEN p.input_evidence_ids IS NOT NULL THEN
           goal_evidence_current(uid,iid,p.parent_goal_id,p.input_evidence_ids)
       ELSE NOT EXISTS (SELECT 1 FROM goal_autonomy_jobs j
           WHERE j.user_id=uid AND j.instance_id=iid AND j.proposal_id=pid)
       END)
$$;
CREATE INDEX goal_autonomy_proposal_scope_idx ON goal_autonomy_jobs
    (user_id,instance_id,proposal_id) WHERE proposal_id IS NOT NULL;

-- The latest research proposal binds both approvals and its original user
-- statement; no older alternative is silently selected.
CREATE FUNCTION strategy_research_sources_current(uid uuid,iid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM goal_strategy_research r
      JOIN goal_strategies s ON s.strategy_id=r.strategy_id AND s.goal_id=r.goal_id
        AND s.user_id=r.user_id AND s.instance_id=r.instance_id
      WHERE r.user_id=uid AND r.instance_id=iid AND r.strategy_id=sid
        AND workflow_source_current(uid,iid,r.research_conversation_id,
                                    r.research_turn_id,r.research_approval_turn_id)
        AND workflow_source_current(uid,iid,r.proposal_conversation_id,
                                    s.source_turn_id,r.proposal_approval_turn_id))
$$;

-- Unlinked explicit platform reminders remain authorized by their own
-- command. Linked notifications depend on the exact authenticated source.
CREATE FUNCTION notification_source_current(uid uuid,iid uuid,cid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT CASE WHEN cid IS NULL AND sid IS NULL THEN true
           ELSE dialogue_turn_current(uid,iid,cid,sid) END
$$;
