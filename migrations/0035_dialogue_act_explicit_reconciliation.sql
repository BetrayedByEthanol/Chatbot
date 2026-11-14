-- MVP-01C review fix: an automatic dialogue proposal must not claim a
-- delivered response forever. Explicit registration can supersede an
-- automatically offered question/decision without erasing audit history.
--
-- The original UNIQUE(source_response_id) prevented replacing even a
-- CANCELLED automatic act. Keep a single PENDING act per response and
-- a single explicitly registered act per response; retain superseded
-- automatic rows for source/evidence and event-history inspection.
ALTER TABLE dialogue_pending_acts
    DROP CONSTRAINT dialogue_pending_acts_source_response_id_key;

CREATE UNIQUE INDEX dialogue_pending_one_active_response_idx
    ON dialogue_pending_acts(source_response_id)
    WHERE status='PENDING';

CREATE UNIQUE INDEX dialogue_pending_one_explicit_response_idx
    ON dialogue_pending_acts(source_response_id)
    WHERE origin_kind='EXPLICIT';
