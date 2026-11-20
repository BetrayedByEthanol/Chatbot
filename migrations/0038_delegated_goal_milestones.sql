-- MVP-03A.1: preserve creation provenance when the Goal owner derives a
-- quantitatively bounded child under an already user-approved objective.
-- Historic goals were all created through the exact authenticated challenge.
ALTER TABLE goal_records
    ADD COLUMN creation_authority text NOT NULL DEFAULT 'USER_APPROVED'
    CHECK (creation_authority IN ('USER_APPROVED','DELEGATED_MILESTONE'));

CREATE INDEX goal_delegated_children_idx
    ON goal_records(parent_goal_id,desired_outcome)
    WHERE creation_authority='DELEGATED_MILESTONE';
