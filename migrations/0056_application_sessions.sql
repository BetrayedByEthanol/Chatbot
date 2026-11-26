-- MVP-07A: operator-issued, scoped, revocable application credentials.
CREATE TABLE application_sessions (
    session_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    selection_generation bigint NOT NULL CHECK(selection_generation>=1),
    token_sha256 text NOT NULL UNIQUE CHECK(token_sha256 ~ '^[0-9a-f]{64}$'),
    issued_by text NOT NULL CHECK(char_length(issued_by) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text CHECK(char_length(revoked_by) BETWEEN 1 AND 128),
    CHECK(expires_at>created_at),
    CHECK((revoked_at IS NULL)=(revoked_by IS NULL)),
    UNIQUE(session_id,user_id,instance_id,conversation_id,selection_generation),
    FOREIGN KEY(user_id,instance_id) REFERENCES persona_instances(user_id,id)
);
CREATE INDEX application_sessions_owner_idx ON application_sessions(user_id,instance_id,created_at);
ALTER TABLE conversation_responses ADD CONSTRAINT conversation_response_application_scope
    UNIQUE(response_id,user_id,instance_id,conversation_id,selection_generation);
CREATE TABLE application_delivery_offers (
    session_id uuid NOT NULL,
    response_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    selection_generation bigint NOT NULL,
    offered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(session_id,response_id),
    FOREIGN KEY(session_id,user_id,instance_id,conversation_id,selection_generation)
        REFERENCES application_sessions(session_id,user_id,instance_id,conversation_id,selection_generation),
    FOREIGN KEY(response_id,user_id,instance_id,conversation_id,selection_generation)
        REFERENCES conversation_responses(response_id,user_id,instance_id,conversation_id,selection_generation)
);
