-- P4-02a: frozen, epoch-scoped input boundaries and a durable stage marker.
-- Epoch entries remain NOT READY for autonomous P2 work. P4-03 owns
-- alignment validation and the only legitimate READY promotion.
ALTER TABLE background_preparation_epochs
  ADD COLUMN preparation_stage text NOT NULL DEFAULT 'SUMMARIZE'
  CHECK (preparation_stage IN (
      'SUMMARIZE', 'EVALUATE', 'CRITICAL_PENDING', 'BLOCKED'
  )),
  ADD COLUMN preparation_updated_at timestamptz NOT NULL DEFAULT clock_timestamp();

CREATE TABLE background_preparation_conversations (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    background_epoch bigint NOT NULL,
    conversation_id uuid NOT NULL,
    -- Includes all primary-user turns committed before Background entry.
    -- Zero means no turns; never use "next_sequence" as the inclusive bound.
    target_sequence bigint NOT NULL CHECK (target_sequence >= 0),
    PRIMARY KEY (user_id, instance_id, background_epoch, conversation_id),
    FOREIGN KEY (user_id, instance_id, background_epoch)
        REFERENCES background_preparation_epochs(user_id, instance_id, background_epoch),
    FOREIGN KEY (conversation_id, user_id, instance_id)
        REFERENCES conversations(conversation_id, user_id, instance_id)
);

CREATE INDEX background_preparation_conversations_lookup_idx
    ON background_preparation_conversations(conversation_id, background_epoch);

-- A full-tail summary must be explicitly authorized at admission, not inferred
-- at worker execution from the current conversation length.
ALTER TABLE conversation_summary_jobs
    ADD COLUMN background_handover_epoch bigint
      CHECK (background_handover_epoch IS NULL OR background_handover_epoch >= 1);

-- P4-01 could already have PENDING epochs before this migration. Their
-- departure-time conversation tails cannot be reconstructed reliably.
-- Fail closed and require a fresh authenticated Background entry.
UPDATE background_preparation_epochs
SET preparation_stage='BLOCKED',
    preparation_updated_at=clock_timestamp()
WHERE status='PENDING';
