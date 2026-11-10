-- P0-03 durable event delivery. No broker/Redis: PostgreSQL is authoritative.
-- Subscription registration is an internal service operation, not event authority.
CREATE TABLE event_consumers (
    consumer_id text PRIMARY KEY CHECK (char_length(consumer_id) BETWEEN 1 AND 128)
);

CREATE TABLE event_subscriptions (
    consumer_id text NOT NULL REFERENCES event_consumers(consumer_id),
    event_type text NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 200),
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (consumer_id, event_type)
);

CREATE TABLE event_deliveries (
    delivery_id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES events(event_id),
    consumer_id text NOT NULL REFERENCES event_consumers(consumer_id),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CLAIMED', 'PROCESSED',
                         'RETRY_PENDING', 'DEFERRED', 'FAILED')),
    eligible_at timestamptz NOT NULL DEFAULT now(),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 30),
    claim_generation bigint NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    worker_id text,
    lease_until timestamptz,
    processed_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, consumer_id),
    CHECK (
        (status = 'CLAIMED' AND worker_id IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status <> 'CLAIMED' AND worker_id IS NULL AND lease_until IS NULL)
    ),
    CHECK (status <> 'PROCESSED' OR processed_at IS NOT NULL)
);

CREATE INDEX event_deliveries_runnable_idx
    ON event_deliveries(consumer_id, eligible_at, delivery_id)
    WHERE status IN ('PENDING', 'RETRY_PENDING', 'CLAIMED');
CREATE INDEX event_deliveries_expired_idx
    ON event_deliveries(lease_until, delivery_id)
    WHERE status = 'CLAIMED';
CREATE INDEX event_deliveries_event_idx
    ON event_deliveries(event_id);
