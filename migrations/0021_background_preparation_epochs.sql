-- P4-01a: one durable, epoch-scoped admission record per Background entry.
-- Persona Runtime creates/supersedes this record in the same transaction as
-- the lifecycle transition. The preparation coordinator is not yet allowed
-- to certify readiness; P0's background_ready_epochs remains the P2 gate.
CREATE TABLE background_preparation_epochs (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    background_epoch bigint NOT NULL CHECK (background_epoch >= 1),
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    presence_revision bigint NOT NULL CHECK (presence_revision >= 1),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SUPERSEDED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    superseded_at timestamptz,
    PRIMARY KEY (user_id, instance_id, background_epoch),
    FOREIGN KEY (user_id, instance_id)
        REFERENCES persona_instances(user_id, id),
    CHECK ((status = 'SUPERSEDED') = (superseded_at IS NOT NULL))
);
CREATE INDEX background_preparation_pending_idx
    ON background_preparation_epochs(user_id, instance_id, background_epoch)
    WHERE status = 'PENDING';
