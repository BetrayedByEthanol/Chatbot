-- P2-F1.6: bounded exact-source review, not another evaluation store.
CREATE INDEX goal_evidence_scope_source_idx ON goal_evidence
    (user_id,instance_id,source_conversation_id,source_turn_id);
CREATE INDEX cognitive_reflections_scope_evidence_idx ON cognitive_reflections
    (user_id,instance_id,conversation_id,evidence_turn_id);
