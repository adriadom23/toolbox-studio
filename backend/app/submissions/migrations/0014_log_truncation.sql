ALTER TABLE submission_log_refs ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0,1));
