-- P4-03 ALIGN-01: change-focused, owner-scoped task findings and epoch certificate.
-- A certificate is not a generic Goal-origin permission; P4-04 owns P2
-- admissions, and the P0 claim gate must additionally check this task ledger.
ALTER TABLE background_preparation_epochs
  DROP CONSTRAINT background_preparation_epochs_preparation_stage_check;
ALTER TABLE background_preparation_epochs
  ADD CONSTRAINT background_preparation_epochs_preparation_stage_check
  CHECK (preparation_stage IN (
    'SUMMARIZE','EVALUATE','CRITICAL_PENDING','ALIGNMENT_PENDING','READY','BLOCKED'
  )),
  ADD COLUMN alignment_last_task_id uuid,
  ADD COLUMN alignment_scanned_count bigint NOT NULL DEFAULT 0
    CHECK (alignment_scanned_count >= 0);

-- Immutable per-epoch review result with explicit task version and owner
-- lineage pins. BLOCKED/REVIEW only gate affected work; there is no heuristic
-- that rewrites the approved Goal/Procedure/Memory records.
CREATE TABLE background_alignment_task_checks (
  user_id uuid NOT NULL,
  instance_id uuid NOT NULL,
  background_epoch bigint NOT NULL,
  task_id uuid NOT NULL,
  task_version bigint NOT NULL CHECK (task_version >= 1),
  goal_id uuid NOT NULL,
  goal_versions jsonb NOT NULL CHECK (jsonb_typeof(goal_versions)='object'),
  procedure_run_id uuid,
  procedure_run_version bigint,
  decision text NOT NULL CHECK (decision IN ('ELIGIBLE','BLOCKED','REVIEW')),
  reason text NOT NULL CHECK (reason IN (
    'VALIDATED','NOT_AGENT_WORK','TASK_INACTIVE','GOAL_INACTIVE',
    'GOAL_LINEAGE_UNVERIFIED','GOAL_LINEAGE_INVALID',
    'PREREQUISITE_UNMET','PRIORITY_SUSPENDED','EXCLUSIVE_OTHER',
    'DEADLINE_EXPIRED','MISSING_APPROVED_RUN','RUN_INACTIVE',
    'PROCEDURE_SCOPE_INVALID','PROCEDURE_REVISION_CHANGED',
    'STRATEGY_CHANGED','STEP_CAPABILITY_MISMATCH','SOURCE_UNVERIFIED'
  )),
  priority_rank integer NOT NULL DEFAULT 1 CHECK(priority_rank BETWEEN 0 AND 2),
  verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id,instance_id,background_epoch,task_id),
  FOREIGN KEY (user_id,instance_id,background_epoch)
      REFERENCES background_preparation_epochs(user_id,instance_id,background_epoch),
  FOREIGN KEY (task_id,user_id,instance_id)
      REFERENCES goal_tasks(task_id,user_id,instance_id),
  FOREIGN KEY (goal_id,user_id,instance_id)
      REFERENCES goal_records(goal_id,user_id,instance_id),
  FOREIGN KEY (procedure_run_id,user_id,instance_id)
      REFERENCES procedure_runs(run_id,user_id,instance_id),
  CHECK ((procedure_run_id IS NULL) = (procedure_run_version IS NULL))
);
CREATE INDEX background_alignment_eligible_idx
  ON background_alignment_task_checks(user_id,instance_id,background_epoch,decision,priority_rank)
  WHERE decision='ELIGIBLE';
CREATE INDEX background_alignment_task_scan_idx
  ON goal_tasks(user_id,instance_id,task_id);

-- Only the Background coordinator may insert this certificate after
-- rechecking the exact frozen conversation watermarks and all task checks.
-- An old READY row cannot be used without the scoped certificate/ledger.
CREATE TABLE background_alignment_certificates (
  user_id uuid NOT NULL,
  instance_id uuid NOT NULL,
  background_epoch bigint NOT NULL,
  selection_generation bigint NOT NULL CHECK(selection_generation>=1),
  checked_tasks bigint NOT NULL CHECK(checked_tasks>=0),
  eligible_tasks bigint NOT NULL CHECK(eligible_tasks>=0),
  blocked_tasks bigint NOT NULL CHECK(blocked_tasks>=0),
  review_tasks bigint NOT NULL CHECK(review_tasks>=0),
  certified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id,instance_id,background_epoch),
  FOREIGN KEY (user_id,instance_id,background_epoch)
    REFERENCES background_preparation_epochs(user_id,instance_id,background_epoch),
  CHECK (checked_tasks=eligible_tasks+blocked_tasks+review_tasks)
);
