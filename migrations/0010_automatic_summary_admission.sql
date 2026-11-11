-- P2-03: persist the original admission origin across stage/enqueue crashes.
-- Existing P2-02 staged records were authorized by an authenticated user.
ALTER TABLE conversation_summary_jobs
    ADD COLUMN admission_origin text NOT NULL DEFAULT 'USER'
        CHECK (admission_origin IN ('USER','COGNITIVE'));
