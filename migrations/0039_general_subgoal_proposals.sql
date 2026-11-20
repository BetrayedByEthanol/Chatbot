-- MVP-03A.2: Goal-owned, non-actionable general subgoal candidates.
-- A pending candidate is NOT a goal and grants no task/execution authority.
ALTER TABLE goal_records DROP CONSTRAINT goal_records_creation_authority_check;
ALTER TABLE goal_records ADD CONSTRAINT goal_records_creation_authority_check
  CHECK (creation_authority IN (
    'USER_APPROVED','DELEGATED_MILESTONE','DELEGATED_VERIFIED'
  ));

CREATE TABLE goal_subgoal_proposals (
    proposal_id uuid PRIMARY KEY,
    proposed_goal_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    parent_goal_id uuid NOT NULL,
    expected_parent_version bigint NOT NULL CHECK (expected_parent_version>=1),
    source_parent_event_id uuid NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    desired_outcome text NOT NULL CHECK (char_length(desired_outcome) BETWEEN 1 AND 600),
    success_criterion text NOT NULL CHECK (char_length(success_criterion) BETWEEN 1 AND 600),
    horizon text NOT NULL CHECK (horizon IN ('SHORT','MEDIUM','LONG','OPEN')),
    contribution_note text NOT NULL CHECK (char_length(contribution_note) BETWEEN 1 AND 500),
    proposal_digest text NOT NULL CHECK (char_length(proposal_digest)=64),
    status text NOT NULL CHECK (status IN ('ACCEPTED','PENDING_REVIEW')),
    decision_method text NOT NULL CHECK (
      decision_method IN ('DETERMINISTIC_COUNT_MILESTONE','REVIEW_REQUIRED')
    ),
    created_event_id uuid NOT NULL,
    accepted_goal_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (user_id,instance_id,parent_goal_id,proposal_digest),
    FOREIGN KEY (parent_goal_id,user_id,instance_id)
      REFERENCES goal_records(goal_id,user_id,instance_id),
    FOREIGN KEY (source_parent_event_id,user_id,instance_id)
      REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (created_event_id,user_id,instance_id)
      REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (accepted_goal_event_id,user_id,instance_id)
      REFERENCES events(event_id,user_id,instance_id),
    CHECK ((status='ACCEPTED' AND accepted_goal_event_id IS NOT NULL)
       OR (status='PENDING_REVIEW' AND accepted_goal_event_id IS NULL))
);
CREATE INDEX goal_subgoal_proposals_pending_idx
  ON goal_subgoal_proposals(user_id,instance_id,parent_goal_id,created_at)
  WHERE status='PENDING_REVIEW';
