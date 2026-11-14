-- MVP-01C: Conversation Manager retains authority for automatic dialogue
-- progress. No change to domain goal/task/procedure authorization.
ALTER TABLE dialogue_threads
  ADD COLUMN origin_kind text NOT NULL DEFAULT 'EXPLICIT'
  CHECK (origin_kind IN ('EXPLICIT','AUTO_SIGNAL'));
ALTER TABLE dialogue_pending_acts
  ADD COLUMN origin_kind text NOT NULL DEFAULT 'EXPLICIT'
  CHECK (origin_kind IN ('EXPLICIT','AUTO_DELIVERED_QUESTION'));

CREATE INDEX dialogue_auto_threads_by_title_idx
  ON dialogue_threads(conversation_id,user_id,instance_id,lower(title))
  WHERE origin_kind='AUTO_SIGNAL';
CREATE INDEX dialogue_auto_pending_idx
  ON dialogue_pending_acts(conversation_id,thread_id,status,created_at)
  WHERE origin_kind='AUTO_DELIVERED_QUESTION' AND status='PENDING';
