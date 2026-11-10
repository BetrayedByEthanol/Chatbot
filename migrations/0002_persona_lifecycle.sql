-- Phase 0 slice 02: presence is versioned independently of selection.
-- User activity in Foreground invalidates stale idle timers without changing
-- execution generation.
ALTER TABLE persona_selection
    ADD COLUMN presence_revision bigint NOT NULL DEFAULT 0
        CHECK (presence_revision >= 0),
    ADD COLUMN last_primary_activity_at timestamptz NOT NULL DEFAULT now();

-- Lifecycle commands store a typed JSON result. Prior rename command receipts
-- retain their existing result_version/result_label fields.
ALTER TABLE command_receipts
    ADD COLUMN result_json jsonb;
