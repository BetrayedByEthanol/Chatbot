-- P0-04: durable, fenced operation scheduler (PostgreSQL authoritative).
-- Task-domain admission gates, active background preparation and external effect
-- reconciler are deliberately separate domain-owned increments.

CREATE TABLE execution_resource_pools (
    resource_key text PRIMARY KEY CHECK (char_length(resource_key) BETWEEN 1 AND 128),
    capacity_units integer NOT NULL CHECK (capacity_units > 0),
    p0_reserved_units integer NOT NULL DEFAULT 0 CHECK (p0_reserved_units >= 0),
    active_units integer NOT NULL DEFAULT 0 CHECK (active_units >= 0),
    active_non_p0_units integer NOT NULL DEFAULT 0 CHECK (active_non_p0_units >= 0),
    CHECK (p0_reserved_units <= capacity_units),
    CHECK (active_units <= capacity_units),
    CHECK (active_non_p0_units <= active_units),
    CHECK (active_non_p0_units <= capacity_units - p0_reserved_units)
);

-- The Background barrier owner may insert READY only after the complete
-- v2.5 summarize -> evaluate -> critical commit -> priority stage chain.
-- P0-04 exposes NO public method to mark a gate READY.
CREATE TABLE background_ready_epochs (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    background_epoch bigint NOT NULL CHECK (background_epoch >= 0),
    ready_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, instance_id, background_epoch),
    FOREIGN KEY (user_id, instance_id) REFERENCES persona_instances(user_id, id)
);

CREATE TABLE operations (
    operation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    owner_kind text NOT NULL DEFAULT 'PERSONA'
        CHECK (owner_kind = 'PERSONA'),
    task_id uuid,
    operation_type text NOT NULL CHECK (char_length(operation_type) BETWEEN 1 AND 128),
    origin text NOT NULL CHECK (origin IN ('USER', 'COGNITIVE', 'GOAL')),
    execution_policy text NOT NULL CHECK (execution_policy IN (
        'FOREGROUND_ASYNC','BACKGROUND_EPHEMERAL','BACKGROUND_RESUMABLE')),
    priority_class text NOT NULL CHECK (priority_class IN ('P0','P1','P2')),
    status text NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN (
            'QUEUED', 'RUNNING', 'RETRY_WAIT', 'CANCEL_REQUESTED',
            'CANCELLED','SUSPEND_REQUESTED','SUSPENDED','COMPLETED',
            'FAILED','BLOCKED','OUTCOME_UNKNOWN')),
    eligible_at timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz,
    timeout_seconds integer NOT NULL DEFAULT 300
        CHECK (timeout_seconds BETWEEN 1 AND 86400),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 30),
    worker_id text,
    claim_generation bigint NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    lease_until timestamptz,
    claimed_at timestamptz,
    selection_generation bigint NOT NULL CHECK (selection_generation >= 1),
    background_epoch bigint,
    checkpoint_ref text,
    result_ref text,
    effect_class text NOT NULL DEFAULT 'NONE'
        CHECK (effect_class IN ('NONE','READ_ONLY','EXTERNAL')),
    effect_status text NOT NULL DEFAULT 'PREPARED'
        CHECK (effect_status IN ('PREPARED','DISPATCHED','CONFIRMED','FAILED','UNKNOWN')),
    request_fingerprint text NOT NULL,
    source_id text NOT NULL,
    idempotency_key text NOT NULL,
    input_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    FOREIGN KEY (user_id, instance_id) REFERENCES persona_instances(user_id, id),
    UNIQUE (user_id, instance_id, idempotency_key),
    CHECK (deadline IS NULL OR deadline > created_at),
    CHECK (
        (status IN ('RUNNING','CANCEL_REQUESTED','SUSPEND_REQUESTED')
         AND worker_id IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status NOT IN ('RUNNING','CANCEL_REQUESTED','SUSPEND_REQUESTED')
         AND worker_id IS NULL AND lease_until IS NULL)
    ),
    CHECK (priority_class <> 'P2' OR execution_policy = 'BACKGROUND_RESUMABLE'),
    CHECK (execution_policy <> 'BACKGROUND_RESUMABLE' OR priority_class = 'P2'),
    CHECK (execution_policy <> 'BACKGROUND_EPHEMERAL' OR priority_class = 'P1')
);

CREATE TABLE operation_dependencies (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    depends_on_id uuid NOT NULL REFERENCES operations(operation_id),
    PRIMARY KEY (operation_id, depends_on_id),
    CHECK (operation_id <> depends_on_id)
);

CREATE TABLE operation_capabilities (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    capability text NOT NULL CHECK (char_length(capability) BETWEEN 1 AND 128),
    PRIMARY KEY (operation_id, capability)
);

CREATE TABLE operation_resource_requests (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    resource_key text NOT NULL REFERENCES execution_resource_pools(resource_key),
    units integer NOT NULL CHECK (units > 0),
    PRIMARY KEY (operation_id, resource_key)
);

CREATE TABLE operation_resource_reservations (
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    resource_key text NOT NULL REFERENCES execution_resource_pools(resource_key),
    claim_generation bigint NOT NULL,
    units integer NOT NULL CHECK (units > 0),
    PRIMARY KEY (operation_id, resource_key)
);

CREATE TABLE operation_concurrency_locks (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    group_key text NOT NULL,
    operation_id uuid NOT NULL UNIQUE REFERENCES operations(operation_id),
    claim_generation bigint NOT NULL,
    PRIMARY KEY (user_id, instance_id, group_key),
    FOREIGN KEY (user_id, instance_id) REFERENCES persona_instances(user_id,id)
);

ALTER TABLE operations ADD COLUMN concurrency_group text
    CHECK (concurrency_group IS NULL OR
           char_length(concurrency_group) BETWEEN 1 AND 128);

CREATE INDEX operation_runnable_idx ON operations (
    priority_class, eligible_at, created_at
) WHERE status IN ('QUEUED','RETRY_WAIT');
CREATE INDEX operation_lease_idx ON operations (lease_until)
    WHERE status IN ('RUNNING','CANCEL_REQUESTED','SUSPEND_REQUESTED');
CREATE INDEX operation_instance_status_idx ON operations (user_id,instance_id,status);
CREATE INDEX operation_depends_idx ON operation_dependencies(depends_on_id);
