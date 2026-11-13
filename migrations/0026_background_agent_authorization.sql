-- P4-04: separately authorized, single-epoch Background dispatch of the
-- existing registered READ_ONLY Procedure AGENT step. No generic tool grant.
ALTER TABLE procedure_agent_operation_authorizations
  ADD COLUMN approved_background_epoch bigint
  CHECK (approved_background_epoch IS NULL OR approved_background_epoch>=1);

-- A user-authorized Background stage is scoped to the NEXT Background epoch
-- observed when the explicit, separately approved command is committed.
-- Existing P3 Foreground stages remain NULL and cannot be promoted to P2.
CREATE INDEX procedure_background_stage_idx
  ON procedure_agent_operation_authorizations(
     user_id,instance_id,approved_background_epoch,state,task_id)
  WHERE approved_background_epoch IS NOT NULL AND state='AUTHORIZED';
