-- P2-07: two authoritative domains with independent versions and evidence.
-- Numeric affect is simulated state, not a verified property of the user.
CREATE TABLE persona_emotion_state (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    valence numeric(5,3) NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
    arousal numeric(5,3) NOT NULL DEFAULT 0.250 CHECK (arousal BETWEEN 0 AND 1),
    engagement numeric(5,3) NOT NULL DEFAULT 0.500 CHECK (engagement BETWEEN 0 AND 1),
    baseline_valence numeric(5,3) NOT NULL DEFAULT 0 CHECK (baseline_valence BETWEEN -1 AND 1),
    baseline_arousal numeric(5,3) NOT NULL DEFAULT 0.250 CHECK (baseline_arousal BETWEEN 0 AND 1),
    baseline_engagement numeric(5,3) NOT NULL DEFAULT 0.500 CHECK (baseline_engagement BETWEEN 0 AND 1),
    last_source_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id,instance_id),
    FOREIGN KEY (user_id,instance_id) REFERENCES persona_instances(user_id,id)
);

CREATE TABLE emotion_appraisals (
    appraisal_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    event_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 200),
    signal_kind text NOT NULL CHECK (signal_kind IN (
        'USER_POSITIVE','USER_NEGATIVE','USER_EXCITED','USER_FRUSTRATED',
        'GRATITUDE','HELPFUL_FEEDBACK','UNHELPFUL_FEEDBACK'
    )),
    user_affect_label text CHECK (user_affect_label IN (
        'HAPPY','SAD','EXCITED','FRUSTRATED'
    )),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    expires_at timestamptz,
    applied boolean NOT NULL,
    state_version bigint,
    epistemic_status text NOT NULL DEFAULT 'TENTATIVE'
        CHECK (epistemic_status='TENTATIVE'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_event_id),
    FOREIGN KEY (user_id,instance_id)
        REFERENCES persona_emotion_state(user_id,instance_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX emotion_appraisals_recent_idx
    ON emotion_appraisals(user_id,instance_id,source_turn_at DESC)
    WHERE user_affect_label IS NOT NULL;

CREATE TABLE relationship_state (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    interaction_count bigint NOT NULL DEFAULT 0 CHECK (interaction_count >= 0),
    familiarity numeric(5,3) NOT NULL DEFAULT 0 CHECK (familiarity BETWEEN 0 AND 1),
    rapport numeric(5,3) NOT NULL DEFAULT 0 CHECK (rapport BETWEEN -1 AND 1),
    last_interaction_at timestamptz,
    last_feedback_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id,instance_id),
    FOREIGN KEY (user_id,instance_id) REFERENCES persona_instances(user_id,id)
);

CREATE TABLE relationship_observations (
    observation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    event_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    feedback text NOT NULL CHECK (feedback IN ('NONE','POSITIVE','NEGATIVE')),
    source_quote text CHECK (source_quote IS NULL OR
        char_length(source_quote) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_event_id),
    FOREIGN KEY (user_id,instance_id)
        REFERENCES relationship_state(user_id,instance_id),
    FOREIGN KEY (conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY (source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id),
    FOREIGN KEY (event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX relationship_observations_recent_idx
    ON relationship_observations(user_id,instance_id,source_turn_at DESC);

-- Privacy INTERNAL: user instructions/boundaries are not general memory facts.
CREATE TABLE relationship_preferences (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    preference_key text NOT NULL CHECK (preference_key IN (
        'RESPONSE_LENGTH','EMOJI_USE','PROACTIVE_MESSAGES','AVOID_PROACTIVE_TOPIC'
    )),
    scope_key text NOT NULL DEFAULT '' CHECK (char_length(scope_key) <= 80),
    preference_value text NOT NULL CHECK (char_length(preference_value) BETWEEN 1 AND 80),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REMOVED')),
    version bigint NOT NULL CHECK (version >= 1),
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_turn_at timestamptz NOT NULL,
    source_quote text NOT NULL CHECK (char_length(source_quote) BETWEEN 1 AND 200),
    source_id text NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 128),
    privacy_class text NOT NULL DEFAULT 'INTERNAL' CHECK (privacy_class='INTERNAL'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id,instance_id,preference_key,scope_key),
    FOREIGN KEY (user_id,instance_id)
        REFERENCES relationship_state(user_id,instance_id),
    FOREIGN KEY (source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    CHECK ((preference_key='AVOID_PROACTIVE_TOPIC' AND
            scope_key<>'' AND preference_value='AVOID')
           OR (preference_key<>'AVOID_PROACTIVE_TOPIC' AND scope_key='' AND
           ((preference_key='RESPONSE_LENGTH' AND preference_value IN ('SHORT','NORMAL','DETAILED'))
           OR (preference_key='EMOJI_USE' AND preference_value IN ('ON','OFF'))
           OR (preference_key='PROACTIVE_MESSAGES' AND preference_value IN ('ALLOW','DISALLOW')))))
);

CREATE TABLE relationship_preference_revisions (
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    preference_key text NOT NULL,
    scope_key text NOT NULL,
    version bigint NOT NULL,
    preference_value text NOT NULL,
    status text NOT NULL,
    source_conversation_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    source_quote text NOT NULL,
    event_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(user_id,instance_id,preference_key,scope_key,version),
    FOREIGN KEY(user_id,instance_id,preference_key,scope_key)
        REFERENCES relationship_preferences(user_id,instance_id,preference_key,scope_key),
    FOREIGN KEY(source_conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
