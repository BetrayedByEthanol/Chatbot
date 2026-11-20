-- MVP-03A.4: one bounded Goal evaluation opportunity per approved goal
-- revision. Jobs bind P2 to an already READY Background epoch, not tools.
CREATE TABLE goal_autonomy_jobs (
  operation_id uuid PRIMARY KEY,
  user_id uuid NOT NULL,
  instance_id uuid NOT NULL,
  goal_id uuid NOT NULL,
  parent_version bigint NOT NULL CHECK(parent_version>=1),
  parent_created_event_id uuid NOT NULL,
  expected_generation bigint NOT NULL CHECK(expected_generation>=1),
  background_epoch bigint NOT NULL CHECK(background_epoch>=1),
  status text NOT NULL DEFAULT 'STAGED'
    CHECK(status IN ('STAGED','NO_PROPOSAL','PROPOSED','STALE')),
  proposed_goal_id uuid,
  proposal_id uuid,
  model_id text,
  staged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  evaluated_at timestamptz,
  UNIQUE(user_id,instance_id,goal_id,parent_version),
  FOREIGN KEY(goal_id,user_id,instance_id)
    REFERENCES goal_records(goal_id,user_id,instance_id),
  FOREIGN KEY(parent_created_event_id,user_id,instance_id)
    REFERENCES events(event_id,user_id,instance_id),
  FOREIGN KEY(user_id,instance_id,background_epoch)
    REFERENCES background_ready_epochs(user_id,instance_id,background_epoch),
  CHECK ((status='PROPOSED')=(proposed_goal_id IS NOT NULL)),
  CHECK ((proposed_goal_id IS NULL)=(proposal_id IS NULL)),
  CHECK ((status='STAGED' AND evaluated_at IS NULL AND model_id IS NULL)
     OR (status<>'STAGED' AND evaluated_at IS NOT NULL))
);
CREATE INDEX goal_autonomy_jobs_unfinished_idx
  ON goal_autonomy_jobs(user_id,instance_id,background_epoch,staged_at)
  WHERE status='STAGED';
