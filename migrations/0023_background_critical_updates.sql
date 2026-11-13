-- P4-02b: bounded, source-linked critical update consolidation.
-- No cognitive proposal may write authoritative Goal, Task, Memory or permission
-- state, and no stage here grants background_ready_epochs.
ALTER TABLE conversations
  ADD COLUMN critical_committed_watermark bigint NOT NULL DEFAULT 0
    CHECK (critical_committed_watermark >= 0 AND critical_committed_watermark < next_sequence);

ALTER TABLE background_preparation_conversations
  ADD COLUMN critical_scan_sequence bigint NOT NULL DEFAULT 0
    CHECK (critical_scan_sequence >= 0 AND critical_scan_sequence <= target_sequence);

ALTER TABLE background_preparation_epochs
  DROP CONSTRAINT background_preparation_epochs_preparation_stage_check;
ALTER TABLE background_preparation_epochs
  ADD CONSTRAINT background_preparation_epochs_preparation_stage_check
    CHECK (preparation_stage IN
      ('SUMMARIZE','EVALUATE','CRITICAL_PENDING','ALIGNMENT_PENDING','BLOCKED'));

-- Only authenticated primary-turn sources can create review candidates.
-- A PENDING candidate never stands in for an owner-domain commit.
CREATE TABLE background_critical_updates (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    background_epoch bigint NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence >= 1),
    domain text NOT NULL CHECK (domain IN ('MEMORY','GOAL','TASK','INSTRUCTION')),
    status text NOT NULL DEFAULT 'PENDING'
      CHECK (status IN ('PENDING','VERIFIED')),
    -- For MEMORY this is a Memory-owned extraction candidate. For GOAL/TASK
    -- this is a domain-owned event, verified against a source-linked table.
    owner_ref uuid,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id,instance_id,background_epoch,source_turn_id,domain),
    FOREIGN KEY (user_id,instance_id,background_epoch,conversation_id)
        REFERENCES background_preparation_conversations
          (user_id,instance_id,background_epoch,conversation_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    CHECK ((status='VERIFIED')=(owner_ref IS NOT NULL AND decided_at IS NOT NULL))
);
CREATE INDEX background_critical_unresolved_idx
  ON background_critical_updates(user_id,instance_id,background_epoch)
  WHERE status='PENDING';

-- Pre-existing pending epochs were frozen by P4-02a, but their input was not
-- critically classified. The new scan starts at zero, preserving all evidence.
-- Only a completed, successfully reconciled epoch can advance its watermarks.
