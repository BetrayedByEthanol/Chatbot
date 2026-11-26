-- MVP-07B2: effective workflow evidence from existing owner-held immutable
-- provenance. Do not rewrite approved plans, completed operations or status.
-- Source policy is the exact Memory policy already deployed in 0060.
CREATE FUNCTION workflow_source_current(uid uuid,iid uuid,cid uuid,sid uuid,aid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT sid IS NOT NULL AND raw_context_sources_current(
        uid,iid,cid,CASE WHEN aid IS NULL OR aid=sid THEN ARRAY[sid] ELSE ARRAY[sid,aid] END)
$$;

CREATE FUNCTION workflow_task_plan_current(uid uuid,iid uuid,tid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM goal_tasks t
        WHERE t.task_id=tid AND t.user_id=uid AND t.instance_id=iid
          AND workflow_source_current(uid,iid,t.source_conversation_id,t.source_turn_id,t.approval_turn_id))
$$;

CREATE FUNCTION workflow_task_report_current(uid uuid,iid uuid,rid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM task_outcome_reports r
        WHERE r.report_id=rid AND r.user_id=uid AND r.instance_id=iid
          AND workflow_source_current(uid,iid,r.source_conversation_id,r.source_turn_id,r.approval_turn_id))
$$;

CREATE FUNCTION workflow_task_confirmation_current(uid uuid,iid uuid,cid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM task_user_confirmations c
        JOIN task_outcome_reports r ON r.report_id=c.report_id AND r.task_id=c.task_id
          AND r.goal_id=c.goal_id AND r.user_id=c.user_id AND r.instance_id=c.instance_id
        WHERE c.confirmation_id=cid AND c.user_id=uid AND c.instance_id=iid
          AND r.report='DONE'
          AND workflow_source_current(uid,iid,c.source_conversation_id,c.source_turn_id,c.approval_turn_id)
          AND workflow_source_current(uid,iid,r.source_conversation_id,r.source_turn_id,r.approval_turn_id))
$$;

-- An exact approval chain, not a new execution grant. Every renewed
-- checkpoint still depends on the raw evidence of its predecessor.
CREATE FUNCTION workflow_agent_grant_current(uid uuid,iid uuid,oid uuid)
RETURNS boolean LANGUAGE sql AS $$
    WITH RECURSIVE chain(operation_id,depth) AS (
        SELECT oid,1
        UNION ALL
        SELECT l.source_operation_id,c.depth+1 FROM chain c
        JOIN procedure_agent_resume_links l ON l.operation_id=c.operation_id
          AND l.user_id=uid AND l.instance_id=iid
        WHERE c.depth<32
    )
    SELECT COALESCE(bool_and(
        EXISTS (SELECT 1 FROM procedure_agent_operation_authorizations a
            WHERE a.operation_id=c.operation_id AND a.user_id=uid AND a.instance_id=iid
              AND workflow_source_current(uid,iid,a.source_conversation_id,a.source_turn_id,a.approval_turn_id))
        AND NOT (c.depth=32 AND EXISTS (
            SELECT 1 FROM procedure_agent_resume_links l WHERE l.operation_id=c.operation_id
              AND l.user_id=uid AND l.instance_id=iid))
    ),false) FROM chain c
$$;

-- Confirmation is not independent of the DONE report it confirmed. A
-- same-status instruction delivery is not a replacement for that report.
-- Later owner corrections also invalidate older events as effective evidence.
CREATE FUNCTION workflow_step_event_current(uid uuid,iid uuid,eid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM procedure_run_step_events e
        WHERE e.step_event_id=eid AND e.user_id=uid AND e.instance_id=iid
          AND workflow_source_current(uid,iid,e.source_conversation_id,e.source_turn_id,e.approval_turn_id)
          AND NOT EXISTS (
              SELECT 1 FROM procedure_run_step_events newer
              WHERE newer.run_id=e.run_id AND newer.step_id=e.step_id
                AND newer.user_id=uid AND newer.instance_id=iid
                AND newer.step_version>e.step_version AND newer.kind='CORRECTED')
          AND (CASE
              WHEN e.kind='USER_CONFIRMED' OR
                   (e.kind='INSTRUCTION_DELIVERED' AND e.new_status<>'INSTRUCTED')
              THEN COALESCE((
                  SELECT workflow_source_current(uid,iid,prior.source_conversation_id,
                                                 prior.source_turn_id,prior.approval_turn_id)
                         AND prior.new_status=CASE WHEN e.kind='USER_CONFIRMED'
                                             THEN 'USER_REPORTED_DONE' ELSE e.new_status END
                  FROM procedure_run_step_events prior
                  WHERE prior.run_id=e.run_id AND prior.step_id=e.step_id
                    AND prior.user_id=uid AND prior.instance_id=iid
                    AND prior.step_version<e.step_version AND prior.kind<>'INSTRUCTION_DELIVERED'
                  ORDER BY prior.step_version DESC LIMIT 1
              ),false)
              ELSE true END)
          AND NOT EXISTS (
              SELECT 1 FROM procedure_agent_operation_authorizations a
              WHERE a.result_event_id=e.event_id AND a.user_id=uid AND a.instance_id=iid
                AND NOT workflow_agent_grant_current(uid,iid,a.operation_id))
    )
$$;

-- Evaluations keep their exact explicit support, plus the effective
-- step heads at the evaluated run version. No fallback to a superseded
-- evaluation and no rewriting an old assessment into a new one.
CREATE FUNCTION workflow_evaluation_current(uid uuid,iid uuid,eid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM procedure_run_evaluations e
        JOIN procedure_runs r ON r.run_id=e.run_id AND r.user_id=e.user_id AND r.instance_id=e.instance_id
        WHERE e.evaluation_id=eid AND e.user_id=uid AND e.instance_id=iid
          AND workflow_source_current(uid,iid,e.source_conversation_id,e.source_turn_id,NULL)
          AND workflow_source_current(uid,iid,r.source_conversation_id,r.source_turn_id,r.approval_turn_id)
          AND NOT EXISTS (
              SELECT 1 FROM procedure_run_evaluations later
              WHERE later.run_id=e.run_id AND later.user_id=uid AND later.instance_id=iid
                AND later.evaluation_revision>e.evaluation_revision)
          AND NOT EXISTS (
              SELECT 1 FROM procedure_evaluation_step_evidence s
              WHERE s.evaluation_id=e.evaluation_id AND s.user_id=uid AND s.instance_id=iid
                AND NOT workflow_step_event_current(uid,iid,s.step_event_id))
          AND NOT EXISTS (
              SELECT 1 FROM (
                  SELECT DISTINCT ON (s.step_id) s.step_event_id,s.new_status
                  FROM procedure_run_step_events s
                  WHERE s.run_id=e.run_id AND s.user_id=uid AND s.instance_id=iid
                    AND s.run_version<=e.run_version
                  ORDER BY s.step_id,s.step_version DESC
              ) heads WHERE heads.new_status NOT IN ('NOT_STARTED','NEEDS_REVIEW')
                AND NOT workflow_step_event_current(uid,iid,heads.step_event_id))
    )
$$;

CREATE FUNCTION workflow_proposal_current(uid uuid,iid uuid,pid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (SELECT 1 FROM procedure_adaptation_proposals p
        WHERE p.proposal_id=pid AND p.user_id=uid AND p.instance_id=iid
          AND workflow_source_current(uid,iid,p.source_conversation_id,p.source_turn_id,NULL)
          AND workflow_evaluation_current(uid,iid,p.evaluation_id))
$$;

-- Manual revisions are freshly approved complete definitions. Applied
-- adaptations additionally inherit the reviewed evaluation and prior
-- revision that justified them. Bounded chain fails closed on overflow.
CREATE FUNCTION workflow_variant_current(uid uuid,iid uuid,vid uuid,rev integer)
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
    SELECT count(*)<=32 AND COALESCE(bool_and(
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

CREATE FUNCTION workflow_run_current(uid uuid,iid uuid,rid uuid)
RETURNS boolean LANGUAGE sql AS $$
    SELECT EXISTS (
        SELECT 1 FROM procedure_runs r
        WHERE r.run_id=rid AND r.user_id=uid AND r.instance_id=iid
          AND workflow_source_current(uid,iid,r.source_conversation_id,r.source_turn_id,r.approval_turn_id)
          AND (r.variant_id IS NULL OR workflow_variant_current(uid,iid,r.variant_id,r.variant_revision))
          AND (r.task_id IS NULL OR workflow_task_plan_current(uid,iid,r.task_id))
          AND NOT EXISTS (
              SELECT 1 FROM procedure_run_steps s
              WHERE s.run_id=r.run_id AND s.user_id=uid AND s.instance_id=iid
                AND s.status NOT IN ('NOT_STARTED','NEEDS_REVIEW')
                AND NOT workflow_step_event_current(uid,iid,s.last_step_event_id))
    )
$$;

-- Existing owner versions do not change if a prerequisite is corrected
-- and subsequently reconfirmed. Pin its confirmation ID at AGENT approval.
-- NULL on legacy grants means unknown; no guessed historical backfill.
ALTER TABLE procedure_agent_operation_authorizations
    ADD COLUMN task_prerequisite_confirmations jsonb
    CHECK(task_prerequisite_confirmations IS NULL OR
          (jsonb_typeof(task_prerequisite_confirmations)='object'
           AND octet_length(task_prerequisite_confirmations::text)<=2048));
CREATE FUNCTION protect_agent_prerequisite_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.task_prerequisite_confirmations IS DISTINCT FROM OLD.task_prerequisite_confirmations
    THEN RAISE EXCEPTION 'agent prerequisite lineage is immutable'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_agent_prerequisite_lineage
    BEFORE UPDATE ON procedure_agent_operation_authorizations
    FOR EACH ROW EXECUTE FUNCTION protect_agent_prerequisite_lineage();
