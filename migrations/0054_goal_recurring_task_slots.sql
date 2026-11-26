-- MVP-06C: a task can contribute one explicitly approved count occurrence.
-- No separate task state, completion counter, or inferred goal achievement.
CREATE TABLE goal_recurring_task_slots (
    task_id uuid PRIMARY KEY,
    goal_id uuid NOT NULL,
    user_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,
    ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 12),
    event_id uuid NOT NULL UNIQUE,
    CHECK(window_end>window_start AND window_end-window_start<=31),
    UNIQUE(goal_id,window_start,ordinal),
    FOREIGN KEY(task_id,goal_id,user_id,instance_id)
        REFERENCES goal_tasks(task_id,goal_id,user_id,instance_id),
    FOREIGN KEY(event_id,user_id,instance_id)
        REFERENCES events(event_id,user_id,instance_id)
);
CREATE INDEX goal_recurring_cycle_idx ON goal_recurring_task_slots(user_id,instance_id,goal_id,window_start);
CREATE FUNCTION reject_recurring_task_slot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'recurring task occurrence binding is immutable';
END;
$$;
CREATE TRIGGER immutable_recurring_task_slot
    BEFORE UPDATE OR DELETE ON goal_recurring_task_slots
    FOR EACH ROW EXECUTE FUNCTION reject_recurring_task_slot_mutation();
