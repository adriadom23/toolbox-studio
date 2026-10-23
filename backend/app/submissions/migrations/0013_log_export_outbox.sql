CREATE TABLE IF NOT EXISTS submission_log_exports (
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','exporting','exported')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(submission_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_submission_log_exports_pending
ON submission_log_exports(status, lease_until, updated_at);
