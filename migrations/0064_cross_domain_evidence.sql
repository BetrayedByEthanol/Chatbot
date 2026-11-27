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
    CHECK (input_evidence_ids IS NULL OR (
        cardinality(input_evidence_ids)<=3 AND array_position(input_evidence_ids,NULL) IS NULL
    ));
CREATE TRIGGER immutable_goal_proposal_evidence
    BEFORE UPDATE OF input_evidence_ids ON goal_subgoal_proposals
    FOR EACH ROW WHEN (OLD.input_evidence_ids IS DISTINCT FROM NEW.input_evidence_ids)
    EXECUTE FUNCTION reject_procedure_history_mutation();

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


-- Exact saved-document version, source approval and passage integrity.
-- The old strategy selection remains recorded; a researched method must
-- not provide new instructions or AGENT authority from withdrawn citations.
CREATE FUNCTION strategy_research_usable(uid uuid,iid uuid,sid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM goal_strategies s
        WHERE s.user_id=uid AND s.instance_id=iid AND s.strategy_id=sid)
    AND (NOT EXISTS (SELECT 1 FROM goal_strategy_research r
        WHERE r.user_id=uid AND r.instance_id=iid AND r.strategy_id=sid)
    OR (
        strategy_research_sources_current(uid,iid,sid)
        AND EXISTS (SELECT 1 FROM goal_strategy_research_sources p
            WHERE p.user_id=uid AND p.instance_id=iid AND p.strategy_id=sid)
        AND NOT EXISTS (
            SELECT 1 FROM goal_strategy_research_sources p
            LEFT JOIN memory_knowledge_documents d ON d.document_id=p.document_id
              AND d.user_id=p.user_id AND d.instance_id=p.instance_id
            LEFT JOIN memory_knowledge_versions v ON v.document_id=p.document_id
              AND v.version=p.version AND v.user_id=p.user_id AND v.instance_id=p.instance_id
            LEFT JOIN memory_knowledge_passages passage ON passage.document_id=p.document_id
              AND passage.version=p.version AND passage.position=p.position
              AND passage.user_id=p.user_id AND passage.instance_id=p.instance_id
            WHERE p.user_id=uid AND p.instance_id=iid AND p.strategy_id=sid
              AND (
                d.document_id IS NULL OR d.current_version<>p.version
                OR d.record_status<>'ACTIVE' OR d.privacy_class<>'NORMAL'
                OR (d.expires_at IS NOT NULL AND d.expires_at<=clock_timestamp())
                OR v.document_id IS NULL OR v.privacy_class<>'NORMAL'
                OR (v.expires_at IS NOT NULL AND v.expires_at<=clock_timestamp())
                OR NOT workflow_source_current(v.user_id,v.instance_id,
                       v.source_conversation_id,v.evidence_turn_id,v.approval_turn_id)
                OR v.content_sha256 IS DISTINCT FROM p.content_sha256
                OR v.content_sha256 IS DISTINCT FROM encode(sha256(convert_to(v.content,'UTF8')),'hex')
                OR passage.document_id IS NULL
                OR passage.passage_text IS DISTINCT FROM substring(
                       v.content FROM passage.start_offset+1 FOR passage.end_offset-passage.start_offset)
              )
        )
    ))
$$;

CREATE OR REPLACE FUNCTION workflow_variant_current(uid uuid,iid uuid,vid uuid,rev integer)
RETURNS boolean LANGUAGE sql AS $$
    WITH RECURSIVE chain(revision,depth) AS (
        SELECT rev,1
        UNION ALL
        SELECT p.expected_revision,c.depth+1 FROM chain c
        JOIN procedure_adaptation_decisions d ON d.variant_id=vid
          AND d.user_id=uid AND d.instance_id=iid AND d.applied_revision=c.revision
          AND d.decision='APPLY'
        JOIN procedure_adaptation_proposals p ON p.proposal_id=d.proposal_id
          AND p.variant_id=d.variant_id AND p.user_id=d.user_id AND p.instance_id=d.instance_id
        WHERE c.depth<32 AND p.expected_revision<c.revision
    )
    SELECT count(*)<=32 AND EXISTS (
        SELECT 1 FROM personalized_procedures v
        WHERE v.variant_id=vid AND v.user_id=uid AND v.instance_id=iid
          AND (v.strategy_id IS NULL OR strategy_research_usable(uid,iid,v.strategy_id))
    ) AND COALESCE(bool_and(
        EXISTS (SELECT 1 FROM personalized_procedure_revisions v
            WHERE v.variant_id=vid AND v.user_id=uid AND v.instance_id=iid
              AND v.revision=c.revision
              AND workflow_source_current(uid,iid,v.source_conversation_id,v.source_turn_id,v.approval_turn_id))
        AND NOT EXISTS (
            SELECT 1 FROM procedure_adaptation_decisions d
            JOIN procedure_adaptation_proposals p ON p.proposal_id=d.proposal_id
              AND p.variant_id=d.variant_id AND p.user_id=d.user_id AND p.instance_id=d.instance_id
            WHERE d.variant_id=vid AND d.user_id=uid AND d.instance_id=iid
              AND d.decision='APPLY' AND d.applied_revision=c.revision
              AND (NOT workflow_source_current(uid,iid,d.source_conversation_id,d.source_turn_id,d.approval_turn_id)
                   OR NOT workflow_proposal_current(uid,iid,p.proposal_id)
                   OR NOT EXISTS (SELECT 1 FROM chain prior
                        WHERE prior.depth=c.depth+1 AND prior.revision=p.expected_revision)))
    ),false) FROM chain c
$$;
