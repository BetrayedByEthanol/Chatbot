-- MVP-07B2: source set of the bounded summary inference, not copied evidence.
-- NULL explicitly denotes a legacy range whose exact projection is unknown.
-- 16 raw anchors plus at most two projected follow-up origins per anchor.
ALTER TABLE conversation_summaries
    ADD COLUMN source_turn_ids uuid[],
    ADD CHECK (source_turn_ids IS NULL OR
        (cardinality(source_turn_ids)<=48 AND array_position(source_turn_ids,NULL) IS NULL));
