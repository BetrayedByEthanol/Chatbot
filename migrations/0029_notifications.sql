-- P4-06: policy-authorized, durable in-app communication.
-- This is an authenticated global user channel, NOT persona execution.
CREATE TABLE notification_policies (
    user_id uuid PRIMARY KEY REFERENCES users(id),
    enabled boolean NOT NULL DEFAULT false,
    timezone text NOT NULL DEFAULT 'UTC'
        CHECK (char_length(timezone) BETWEEN 1 AND 100),
    quiet_start_min integer CHECK (quiet_start_min BETWEEN 0 AND 1439),
    quiet_end_min integer CHECK (quiet_end_min BETWEEN 0 AND 1439),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((quiet_start_min IS NULL)=(quiet_end_min IS NULL))
);

CREATE TABLE notification_intents (
    intent_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('REMINDER','FOLLOW_UP')),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 500),
    timezone text NOT NULL CHECK (char_length(timezone) BETWEEN 1 AND 100),
    local_hour integer NOT NULL CHECK (local_hour BETWEEN 0 AND 23),
    local_minute integer NOT NULL CHECK (local_minute BETWEEN 0 AND 59),
    recurrence text NOT NULL CHECK (recurrence IN ('NONE','DAILY')),
    next_due_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    source_conversation_id uuid,
    source_turn_id uuid,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    created_event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (intent_id,user_id,instance_id),
    UNIQUE (user_id,instance_id,idempotency_key),
    FOREIGN KEY (user_id,instance_id) REFERENCES persona_instances(user_id,id),
    FOREIGN KEY (source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (created_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    CHECK ((source_conversation_id IS NULL)=(source_turn_id IS NULL))
);
CREATE INDEX notification_due_idx ON notification_intents(next_due_at,intent_id)
    WHERE active;

CREATE TABLE notification_occurrences (
    occurrence_id uuid PRIMARY KEY,
    intent_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'AVAILABLE'
        CHECK (status IN ('AVAILABLE','SNOOZED','ACKNOWLEDGED','CANCELLED')),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    snoozed_until timestamptz,
    acknowledged_at timestamptz,
    UNIQUE (intent_id,scheduled_for),
    UNIQUE (occurrence_id,user_id,instance_id),
    FOREIGN KEY (intent_id,user_id,instance_id)
        REFERENCES notification_intents(intent_id,user_id,instance_id),
    CHECK ((status='SNOOZED')=(snoozed_until IS NOT NULL)),
    CHECK (status<>'ACKNOWLEDGED' OR acknowledged_at IS NOT NULL)
);
CREATE INDEX notification_inbox_idx
    ON notification_occurrences(user_id,status,available_at DESC);
CREATE INDEX notification_snooze_due_idx
    ON notification_occurrences(snoozed_until,occurrence_id)
    WHERE status='SNOOZED';

-- Authenticated replies remain a platform mailbox until the originating
-- persona is explicitly reactivated. No callback changes persona selection.
CREATE TABLE notification_inbound_replies (
    reply_id uuid PRIMARY KEY,
    occurrence_id uuid NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    reply_text text NOT NULL CHECK (char_length(reply_text) BETWEEN 1 AND 1000),
    state text NOT NULL DEFAULT 'PENDING_PERSONA'
        CHECK (state IN ('PENDING_PERSONA','CONSUMED')),
    event_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (occurrence_id,user_id,instance_id)
        REFERENCES notification_occurrences(occurrence_id,user_id,instance_id),
    FOREIGN KEY (event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX notification_pending_persona_idx
    ON notification_inbound_replies(user_id,instance_id,created_at)
    WHERE state='PENDING_PERSONA';
