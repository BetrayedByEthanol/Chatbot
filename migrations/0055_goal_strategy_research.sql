-- MVP-06D: source-backed strategy metadata and immutable source pins.
-- No copies of retrieved document content, no external tool authority.
CREATE TABLE goal_strategy_research (
    strategy_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    goal_version bigint NOT NULL CHECK(goal_version>=1),
    query text NOT NULL CHECK(char_length(query) BETWEEN 2 AND 100),
    limitations text NOT NULL CHECK(char_length(limitations) BETWEEN 10 AND 600),
    research_conversation_id uuid NOT NULL,
    research_turn_id uuid NOT NULL,
    research_approval_turn_id uuid NOT NULL,
    proposal_approval_turn_id uuid NOT NULL,
    proposal_conversation_id uuid NOT NULL,
    event_id uuid NOT NULL,
    UNIQUE(strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(strategy_id,goal_id,user_id,instance_id)
        REFERENCES goal_strategies(strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(research_conversation_id,research_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(research_conversation_id,research_approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(proposal_conversation_id,proposal_approval_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id) REFERENCES events(event_id,user_id,instance_id)
);
CREATE TABLE goal_strategy_research_sources (
    strategy_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 4),
    document_id uuid NOT NULL,
    version bigint NOT NULL CHECK(version>=1),
    position integer NOT NULL CHECK(position>=1),
    content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(strategy_id,ordinal),
    UNIQUE(strategy_id,document_id,version,position),
    FOREIGN KEY(strategy_id,goal_id,user_id,instance_id)
        REFERENCES goal_strategy_research(strategy_id,goal_id,user_id,instance_id),
    FOREIGN KEY(document_id,version,user_id,instance_id)
        REFERENCES memory_knowledge_versions(document_id,version,user_id,instance_id),
    FOREIGN KEY(document_id,version,position)
        REFERENCES memory_knowledge_passages(document_id,version,position)
);
CREATE TRIGGER immutable_strategy_research BEFORE UPDATE OR DELETE ON goal_strategy_research
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
CREATE TRIGGER immutable_strategy_research_sources BEFORE UPDATE OR DELETE ON goal_strategy_research_sources
    FOR EACH ROW EXECUTE FUNCTION reject_procedure_history_mutation();
