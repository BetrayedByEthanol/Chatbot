-- v2.5 / Phase 0 slice 01. Apply once to an empty PostgreSQL database.
-- Unselected instances are logically INACTIVE; lifecycle transitions follow.
CREATE TABLE users (
    id uuid PRIMARY KEY
);

CREATE TABLE persona_definitions (
    id uuid PRIMARY KEY,
    stable_key text NOT NULL UNIQUE
);

CREATE TABLE persona_instances (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    persona_id uuid NOT NULL REFERENCES persona_definitions(id),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 80),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    UNIQUE (user_id, persona_id),
    UNIQUE (user_id, id)
);

-- One selected instance per user. Switches exclusively lock this row,
-- increment generation, and change instance_id atomically.
CREATE TABLE persona_selection (
    user_id uuid PRIMARY KEY REFERENCES users(id),
    instance_id uuid NOT NULL,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
    mode text NOT NULL CHECK (mode IN ('FOREGROUND', 'BACKGROUND')),
    background_epoch bigint NOT NULL DEFAULT 0 CHECK (background_epoch >= 0),
    FOREIGN KEY (user_id, instance_id)
        REFERENCES persona_instances(user_id, id)
);

CREATE TABLE command_receipts (
    receipt_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    result_version bigint,
    result_label text,
    event_id uuid,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, instance_id, command_type, idempotency_key),
    FOREIGN KEY (user_id, instance_id)
        REFERENCES persona_instances(user_id, id)
);

CREATE TABLE event_streams (
    stream_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    domain text NOT NULL,
    last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    UNIQUE (instance_id, domain),
    UNIQUE (stream_id, user_id, instance_id),
    FOREIGN KEY (user_id, instance_id)
        REFERENCES persona_instances(user_id, id)
);

CREATE TABLE events (
    event_id uuid PRIMARY KEY,
    event_type text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version > 0),
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    stream_id uuid NOT NULL,
    stream_sequence bigint NOT NULL CHECK (stream_sequence >= 1),
    source_id text NOT NULL,
    source_event_key text,
    trust_level text NOT NULL,
    correlation_id uuid,
    causation_id uuid,
    occurred_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb,
    payload_ref text,
    idempotency_key text,
    UNIQUE (stream_id, stream_sequence),
    UNIQUE (event_id, user_id, instance_id),
    FOREIGN KEY (stream_id, user_id, instance_id)
        REFERENCES event_streams(stream_id, user_id, instance_id),
    FOREIGN KEY (user_id, instance_id)
        REFERENCES persona_instances(user_id, id)
);

CREATE TABLE outbox (
    outbox_id uuid PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE REFERENCES events(event_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz
);

ALTER TABLE command_receipts
    ADD CONSTRAINT command_receipts_event_fk
    FOREIGN KEY (event_id, user_id, instance_id)
        REFERENCES events(event_id, user_id, instance_id);

CREATE INDEX events_instance_recorded_idx
    ON events(instance_id, recorded_at);
CREATE INDEX outbox_undispatched_idx
    ON outbox(created_at) WHERE dispatched_at IS NULL;
