-- MVP-03A.3: Goal-owned semantic review, never a model-issued command.
-- Existing pending proposals stay non-actionable until this owner transition.
ALTER TABLE goal_subgoal_proposals
  DROP CONSTRAINT goal_subgoal_proposals_status_check;
ALTER TABLE goal_subgoal_proposals
  ADD CONSTRAINT goal_subgoal_proposals_status_check
  CHECK (status IN (
    'ACCEPTED','PENDING_REVIEW','DEFERRED','REJECTED','SUPERSEDED'
  ));
ALTER TABLE goal_subgoal_proposals
  DROP CONSTRAINT goal_subgoal_proposals_check;
ALTER TABLE goal_subgoal_proposals
  ADD CONSTRAINT goal_subgoal_proposals_check
  CHECK (
    (status='ACCEPTED' AND accepted_goal_event_id IS NOT NULL)
    OR (status<>'ACCEPTED' AND accepted_goal_event_id IS NULL)
  );

-- Each proposal has one terminal semantic assessment. A retry uses the
-- previous recorded outcome and cannot re-adopt the same candidate.
CREATE TABLE goal_subgoal_semantic_reviews (
  review_id uuid PRIMARY KEY,
  proposal_id uuid NOT NULL UNIQUE,
  user_id uuid NOT NULL,
  instance_id uuid NOT NULL,
  parent_goal_id uuid NOT NULL,
  reviewed_parent_version bigint NOT NULL CHECK(reviewed_parent_version>=1),
  source_parent_event_id uuid NOT NULL,
  finding text NOT NULL CHECK (
    finding IN ('ALIGNED','UNCERTAIN','DRIFT_DETECTED','BLOCKED')
  ),
  owner_decision text NOT NULL CHECK (
    owner_decision IN ('ACCEPTED','DEFERRED','REJECTED','SUPERSEDED')
  ),
  model_id text NOT NULL CHECK(char_length(model_id) BETWEEN 1 AND 128),
  review_method_version text NOT NULL DEFAULT 'mvp03a3.semantic.v1',
  parent_evidence_quote text NOT NULL CHECK(char_length(parent_evidence_quote) BETWEEN 1 AND 300),
  child_evidence_quote text NOT NULL CHECK(char_length(child_evidence_quote) BETWEEN 1 AND 300),
  contribution_reason text NOT NULL CHECK(char_length(contribution_reason) BETWEEN 1 AND 500),
  evaluation_flags jsonb NOT NULL CHECK(jsonb_typeof(evaluation_flags)='object'),
  event_id uuid NOT NULL,
  accepted_goal_event_id uuid,
  reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY(proposal_id) REFERENCES goal_subgoal_proposals(proposal_id),
  FOREIGN KEY(parent_goal_id,user_id,instance_id)
    REFERENCES goal_records(goal_id,user_id,instance_id),
  FOREIGN KEY(source_parent_event_id,user_id,instance_id)
    REFERENCES events(event_id,user_id,instance_id),
  FOREIGN KEY(event_id,user_id,instance_id)
    REFERENCES events(event_id,user_id,instance_id),
  FOREIGN KEY(accepted_goal_event_id,user_id,instance_id)
    REFERENCES events(event_id,user_id,instance_id),
  CHECK (
    (owner_decision='ACCEPTED' AND accepted_goal_event_id IS NOT NULL)
    OR (owner_decision<>'ACCEPTED' AND accepted_goal_event_id IS NULL)
  )
);
