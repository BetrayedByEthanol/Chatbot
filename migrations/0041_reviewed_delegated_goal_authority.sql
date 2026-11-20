-- Review-produced descendants retain delegated, never user-affirmed authority.
ALTER TABLE goal_records
  DROP CONSTRAINT goal_records_creation_authority_check;
ALTER TABLE goal_records
  ADD CONSTRAINT goal_records_creation_authority_check
  CHECK (creation_authority IN (
    'USER_APPROVED','DELEGATED_MILESTONE','DELEGATED_VERIFIED',
    'DELEGATED_REVIEWED'
  ));
