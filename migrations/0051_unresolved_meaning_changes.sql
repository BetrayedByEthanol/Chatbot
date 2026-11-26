-- MVP-05E: unresolved explicit meaning changes are read barriers, not facts.
CREATE TABLE memory_meaning_change_notices (
    source_turn_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    predicate_key text NOT NULL CHECK(predicate_key IN (
        'meaning_home','meaning_friendship','meaning_learning',
        'meaning_morning_routine','meaning_evening_routine',
        'meaning_weekend_routine','meaning_bedtime_routine'
    )),
    FOREIGN KEY(conversation_id,source_turn_id,user_id,instance_id)
        REFERENCES conversation_turns(conversation_id,turn_id,user_id,instance_id),
    FOREIGN KEY(source_event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX memory_meaning_change_notices_slot_idx
    ON memory_meaning_change_notices(user_id,instance_id,predicate_key);
CREATE CONSTRAINT TRIGGER historical_recall_meaning_notice_changed
    AFTER INSERT OR UPDATE OR DELETE ON memory_meaning_change_notices
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION bump_historical_recall_revision();
